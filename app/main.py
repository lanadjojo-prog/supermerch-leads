from contextlib import asynccontextmanager
import logging
import threading
from secrets import compare_digest
from pathlib import Path
from urllib.parse import quote, quote_plus

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .auth import require_basic_auth
from .chat_api import router as chat_api_router
from .command_bridge import process_startup_command
from .config import settings
from .database import Base, SessionLocal, engine
from .models import Campaign, Lead, MailIntegration
from .services import zoho_mail
from .services.automation_scheduler import run_automation_scheduler, run_current_automation_slot
from .services.intelligence import generate_outreach
from .services.outreach_delivery import DailySendLimitReached, send_lead, sent_today
from .services.pipeline import run_campaign

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        stale = db.scalars(select(Lead).where(Lead.status == "approved")).all()
        changed = 0
        for lead in stale:
            if not lead.email or not lead.outreach_text:
                lead.status = "needs_attention"
                changed += 1
        if changed:
            db.commit()
            logger.info("Moved %s non-sendable approved lead(s) to needs_attention", changed)
    finally:
        db.close()

    scheduler_stop = threading.Event()
    scheduler_thread = threading.Thread(
        target=run_automation_scheduler,
        args=(scheduler_stop,),
        name="supermerch-daily-automation",
        daemon=True,
    )
    scheduler_thread.start()
    try:
        yield
    finally:
        scheduler_stop.set()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(chat_api_router)
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def auth(request: Request):
    require_basic_auth(request)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/internal/daily-lead-engine")
def internal_daily_lead_engine(request: Request, db: Session = Depends(get_db)):
    token = request.headers.get("X-Automation-Token", "")
    if not settings.daily_job_token or not compare_digest(token, settings.daily_job_token):
        raise HTTPException(401, "Unauthorized")
    return run_current_automation_slot(db)


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
def dashboard(request: Request, db: Session = Depends(get_db)):
    campaigns = db.scalars(select(Campaign).order_by(Campaign.created_at.desc())).all()
    counts = dict(db.execute(select(Lead.status, func.count(Lead.id)).group_by(Lead.status)).all())
    top_leads = db.scalars(select(Lead).where(Lead.status == "ready_for_review").order_by(Lead.score.desc(), Lead.created_at.desc()).limit(10)).all()
    return templates.TemplateResponse(request, "dashboard.html", {"campaigns": campaigns, "counts": counts, "top_leads": top_leads, "app_name": settings.app_name, "places_ready": bool(settings.google_places_api_key), "ai_ready": bool(settings.openai_api_key), "zoho_connected": zoho_mail.connected(db), "sent_today": sent_today(db), "daily_target": settings.daily_send_target})


@app.get("/campaigns/new", response_class=HTMLResponse, dependencies=[Depends(auth)])
def new_campaign(request: Request):
    return templates.TemplateResponse(request, "campaign_new.html", {"app_name": settings.app_name})


@app.post("/campaigns", dependencies=[Depends(auth)])
def create_campaign(niche: str = Form(...), region: str = Form("Nederland"), search_query: str = Form(...), target_count: int = Form(25), db: Session = Depends(get_db)):
    campaign = Campaign(name=f"{niche.strip()} – {region.strip()}", niche=niche.strip(), region=region.strip(), search_query=search_query.strip(), target_count=max(1, min(target_count, 60)))
    db.add(campaign); db.commit(); db.refresh(campaign)
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


def _run_campaign_background(campaign_id: int):
    db = SessionLocal()
    try:
        campaign = db.get(Campaign, campaign_id)
        if campaign: run_campaign(db, campaign)
    finally: db.close()


@app.post("/campaigns/{campaign_id}/run", dependencies=[Depends(auth)])
def start_campaign(campaign_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign: raise HTTPException(404)
    if campaign.status == "running": return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)
    campaign.status = "queued"; db.commit(); background_tasks.add_task(_run_campaign_background, campaign_id)
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse, dependencies=[Depends(auth)])
def campaign_detail(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign: raise HTTPException(404)
    leads = db.scalars(select(Lead).where(Lead.campaign_id == campaign_id).order_by(Lead.score.desc(), Lead.created_at.desc())).all()
    return templates.TemplateResponse(request, "campaign_detail.html", {"campaign": campaign, "leads": leads, "app_name": settings.app_name})


@app.get("/leads/{lead_id}", response_class=HTMLResponse, dependencies=[Depends(auth)])
def lead_detail(request: Request, lead_id: int, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead: raise HTTPException(404)
    mailto = None
    if lead.email and lead.outreach_text:
        mailto = f"mailto:{lead.email}?subject={quote('Merchandise voor ' + lead.company_name, safe='')}&body={quote(lead.outreach_text, safe='')}"
    return templates.TemplateResponse(request, "lead_detail.html", {"lead": lead, "mailto": mailto, "app_name": settings.app_name, "zoho_connected": zoho_mail.connected(db), "send_message": request.query_params.get("message"), "send_error": request.query_params.get("error") == "1"})


@app.post("/leads/{lead_id}/status", dependencies=[Depends(auth)])
def set_lead_status(lead_id: int, status_value: str = Form(...), db: Session = Depends(get_db)):
    allowed = {"approved", "rejected", "contacted", "ready_for_review", "possible"}
    if status_value not in allowed: raise HTTPException(400, "Ongeldige status")
    lead = db.get(Lead, lead_id)
    if not lead: raise HTTPException(404)
    lead.status = status_value; db.commit()
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@app.post("/leads/approve-all-review", dependencies=[Depends(auth)])
def approve_all_review(db: Session = Depends(get_db)):
    ready = db.scalars(select(Lead).where(Lead.status == "ready_for_review")).all()
    approved = needs_attention = 0
    for lead in ready:
        if lead.email and lead.outreach_text:
            lead.status = "approved"
            approved += 1
        else:
            lead.status = "needs_attention"
            needs_attention += 1
    db.commit()
    message = f"{approved} lead(s) goedgekeurd"
    if needs_attention:
        message += f"; {needs_attention} naar Actie nodig"
    message += "."
    return RedirectResponse(f"/?message={quote_plus(message)}", status_code=303)


@app.get("/integrations", response_class=HTMLResponse, dependencies=[Depends(auth)])
def integrations(request: Request, db: Session = Depends(get_db)):
    integration = db.scalar(select(MailIntegration).where(MailIntegration.provider == "zoho"))
    return templates.TemplateResponse(request, "integrations.html", {"app_name": settings.app_name, "configured": zoho_mail.configured(), "connected": integration is not None, "integration": integration, "redirect_uri": f"{settings.app_base_url}/integrations/zoho/callback"})


@app.get("/integrations/zoho/connect", dependencies=[Depends(auth)])
def zoho_connect():
    return RedirectResponse(zoho_mail.authorization_url(f"{settings.app_base_url}/integrations/zoho/callback"), status_code=302)


@app.get("/integrations/zoho/callback", name="zoho_callback")
def zoho_callback(code: str | None = None, state: str | None = None, error: str | None = None, db: Session = Depends(get_db)):
    if error: return RedirectResponse(f"/integrations?error=1&message={quote_plus('Zoho toestemming is niet afgerond: ' + error)}", status_code=303)
    if not code or not state or not zoho_mail.validate_state(state): return RedirectResponse(f"/integrations?error=1&message={quote_plus('Ongeldige of verlopen Zoho-koppeling. Probeer opnieuw.')}", status_code=303)
    try:
        integration = zoho_mail.save_connection(db, zoho_mail.exchange_code(code, f"{settings.app_base_url}/integrations/zoho/callback"))
        return RedirectResponse(f"/integrations?message={quote_plus('Zoho Mail verbonden met ' + integration.email_address)}", status_code=303)
    except Exception as exc: return RedirectResponse(f"/integrations?error=1&message={quote_plus('Zoho koppelen mislukt: ' + str(exc))}", status_code=303)


@app.post("/integrations/zoho/disconnect", dependencies=[Depends(auth)])
def zoho_disconnect(db: Session = Depends(get_db)):
    zoho_mail.disconnect(db); return RedirectResponse("/integrations", status_code=303)


@app.post("/leads/send-all-approved", dependencies=[Depends(auth)])
def send_all_approved_via_zoho(db: Session = Depends(get_db)):
    if not zoho_mail.connected(db):
        return RedirectResponse(f"/?error=1&message={quote_plus('Zoho Mail is niet verbonden.')}", status_code=303)

    leads = db.scalars(
        select(Lead).where(Lead.status == "approved").order_by(Lead.created_at.asc())
    ).all()
    if not leads:
        return RedirectResponse(f"/?message={quote_plus('Er staan geen goedgekeurde mails klaar.')}", status_code=303)

    sent = needs_attention = send_failed = 0
    limit_reached = False

    for lead in leads:
        if not lead.email or not lead.outreach_text:
            lead.status = "needs_attention"
            db.commit()
            needs_attention += 1
            continue
        try:
            if send_lead(db, lead):
                sent += 1
        except DailySendLimitReached:
            limit_reached = True
            break
        except Exception as exc:
            logger.exception("Zoho send failed for lead %s (%s): %s", lead.id, lead.company_name, exc)
            send_failed += 1

    message = f"{sent} mail(s) verzonden"
    if needs_attention:
        message += f"; {needs_attention} naar Actie nodig"
    if send_failed:
        message += f"; {send_failed} naar Verzendfout"
    if limit_reached:
        message += f"; daglimiet van {settings.daily_send_target} bereikt"
    message += "."

    return RedirectResponse(
        f"/?{'error=1&' if (needs_attention or send_failed) else ''}message={quote_plus(message)}",
        status_code=303,
    )


@app.post("/leads/{lead_id}/send-zoho", dependencies=[Depends(auth)])
def send_lead_via_zoho(lead_id: int, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404)
    if lead.status != "approved":
        return RedirectResponse(
            f"/leads/{lead_id}?error=1&message={quote_plus('Keur deze lead eerst goed.')}",
            status_code=303,
        )
    if not lead.email or not lead.outreach_text:
        return RedirectResponse(
            f"/leads/{lead_id}?error=1&message={quote_plus('E-mailadres of conceptmail ontbreekt.')}",
            status_code=303,
        )
    try:
        send_lead(db, lead)
        return RedirectResponse(
            f"/leads/{lead_id}?message={quote_plus('E-mail is verzonden via Zoho Mail.')}",
            status_code=303,
        )
    except DailySendLimitReached:
        return RedirectResponse(
            f"/leads/{lead_id}?error=1&message={quote_plus('Daglimiet van ' + str(settings.daily_send_target) + ' mails is bereikt.')}",
            status_code=303,
        )
    except Exception as exc:
        logger.exception("Zoho send failed for lead %s (%s): %s", lead_id, lead.company_name, exc)
        return RedirectResponse(
            f"/leads/{lead_id}?error=1&message={quote_plus('Verzenden mislukt. De lead staat nu bij Verzendfout.')}",
            status_code=303,
        )


@app.post("/leads/{lead_id}/regenerate-outreach", dependencies=[Depends(auth)])
def regenerate_lead_outreach(lead_id: int, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead: raise HTTPException(404)
    analysis = {"industry": lead.industry, "company_summary": lead.company_summary, "employee_signal": lead.employee_signal, "vacancies_signal": lead.vacancies_signal, "employer_branding_signal": lead.employer_branding_signal, "event_signal": lead.event_signal, "growth_signal": lead.growth_signal, "merch_signal": lead.merch_signal, "recommended_offer": "Custom kleding & merchandise in eigen huisstijl", "lead_reason": lead.lead_reason}
    lead.recommended_offer = "Custom kleding & merchandise in eigen huisstijl"
    lead.outreach_text = generate_outreach(lead.company_name, lead.contact_name, analysis); db.commit()
    return RedirectResponse(f"/leads/{lead_id}?message={quote_plus('Nieuw outreachconcept gemaakt.')}", status_code=303)
