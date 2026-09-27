from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, quote_plus

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import require_basic_auth
from .config import settings
from .database import Base, SessionLocal, engine
from .models import Campaign, Lead, MailIntegration
from .services import zoho_mail
from .services.intelligence import generate_outreach
from .services.pipeline import run_campaign


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
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


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
def dashboard(request: Request, db: Session = Depends(get_db)):
    campaigns = db.scalars(select(Campaign).order_by(Campaign.created_at.desc())).all()
    counts = dict(db.execute(select(Lead.status, func.count(Lead.id)).group_by(Lead.status)).all())
    top_leads = db.scalars(
        select(Lead).where(Lead.status == "ready_for_review")
        .order_by(Lead.score.desc(), Lead.created_at.desc()).limit(10)
    ).all()
    return templates.TemplateResponse(request, "dashboard.html", {
        "campaigns": campaigns, "counts": counts, "top_leads": top_leads,
        "app_name": settings.app_name,
        "places_ready": bool(settings.google_places_api_key),
        "ai_ready": bool(settings.openai_api_key),
        "zoho_connected": zoho_mail.connected(db),
    })


@app.get("/campaigns/new", response_class=HTMLResponse, dependencies=[Depends(auth)])
def new_campaign(request: Request):
    return templates.TemplateResponse(request, "campaign_new.html", {"app_name": settings.app_name})


@app.post("/campaigns", dependencies=[Depends(auth)])
def create_campaign(
    niche: str = Form(...),
    region: str = Form("Nederland"),
    search_query: str = Form(...),
    target_count: int = Form(25),
    db: Session = Depends(get_db),
):
    campaign = Campaign(
        name=f"{niche.strip()} – {region.strip()}",
        niche=niche.strip(),
        region=region.strip(),
        search_query=search_query.strip(),
        target_count=max(1, min(target_count, 60)),
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


def _run_campaign_background(campaign_id: int):
    db = SessionLocal()
    try:
        campaign = db.get(Campaign, campaign_id)
        if campaign:
            run_campaign(db, campaign)
    finally:
        db.close()


@app.post("/campaigns/{campaign_id}/run", dependencies=[Depends(auth)])
def start_campaign(campaign_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404)
    if campaign.status == "running":
        return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)
    campaign.status = "queued"
    db.commit()
    background_tasks.add_task(_run_campaign_background, campaign_id)
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse, dependencies=[Depends(auth)])
def campaign_detail(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404)
    leads = db.scalars(
        select(Lead).where(Lead.campaign_id == campaign_id)
        .order_by(Lead.score.desc(), Lead.created_at.desc())
    ).all()
    return templates.TemplateResponse(request, "campaign_detail.html", {
        "campaign": campaign, "leads": leads, "app_name": settings.app_name,
    })


@app.get("/leads/{lead_id}", response_class=HTMLResponse, dependencies=[Depends(auth)])
def lead_detail(request: Request, lead_id: int, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404)
    mailto = None
    if lead.email and lead.outreach_text:
        mailto = (
            f"mailto:{lead.email}?subject={quote('Merchandise voor ' + lead.company_name, safe='')}"
            f"&body={quote(lead.outreach_text, safe='')}"
        )
    send_message = request.query_params.get("message")
    send_error = request.query_params.get("error") == "1"
    return templates.TemplateResponse(request, "lead_detail.html", {
        "lead": lead, "mailto": mailto, "app_name": settings.app_name,
        "zoho_connected": zoho_mail.connected(db),
        "send_message": send_message,
        "send_error": send_error,
    })


@app.post("/leads/{lead_id}/status", dependencies=[Depends(auth)])
def set_lead_status(lead_id: int, status_value: str = Form(...), db: Session = Depends(get_db)):
    allowed = {"approved", "rejected", "contacted", "ready_for_review", "possible"}
    if status_value not in allowed:
        raise HTTPException(400, "Ongeldige status")
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404)
    lead.status = status_value
    db.commit()
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)



@app.get("/integrations", response_class=HTMLResponse, dependencies=[Depends(auth)])
def integrations(request: Request, db: Session = Depends(get_db)):
    integration = db.scalar(select(MailIntegration).where(MailIntegration.provider == "zoho"))
    return templates.TemplateResponse(request, "integrations.html", {
        "app_name": settings.app_name,
        "configured": zoho_mail.configured(),
        "connected": integration is not None,
        "integration": integration,
        "redirect_uri": f"{settings.app_base_url}/integrations/zoho/callback",
    })


@app.get("/integrations/zoho/connect", dependencies=[Depends(auth)])
def zoho_connect():
    redirect_uri = f"{settings.app_base_url}/integrations/zoho/callback"
    return RedirectResponse(zoho_mail.authorization_url(redirect_uri), status_code=302)


@app.get("/integrations/zoho/callback", name="zoho_callback")
def zoho_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    if error:
        return RedirectResponse(
            f"/integrations?error=1&message={quote_plus('Zoho toestemming is niet afgerond: ' + error)}",
            status_code=303,
        )
    if not code or not state or not zoho_mail.validate_state(state):
        return RedirectResponse(
            f"/integrations?error=1&message={quote_plus('Ongeldige of verlopen Zoho-koppeling. Probeer opnieuw.')}",
            status_code=303,
        )
    redirect_uri = f"{settings.app_base_url}/integrations/zoho/callback"
    try:
        token_payload = zoho_mail.exchange_code(code, redirect_uri)
        integration = zoho_mail.save_connection(db, token_payload)
        return RedirectResponse(
            f"/integrations?message={quote_plus('Zoho Mail verbonden met ' + integration.email_address)}",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            f"/integrations?error=1&message={quote_plus('Zoho koppelen mislukt: ' + str(exc))}",
            status_code=303,
        )


@app.post("/integrations/zoho/disconnect", dependencies=[Depends(auth)])
def zoho_disconnect(db: Session = Depends(get_db)):
    zoho_mail.disconnect(db)
    return RedirectResponse("/integrations", status_code=303)


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
        zoho_mail.send_email(
            db,
            to_address=lead.email,
            subject=f"Merchandise voor {lead.company_name}",
            content=lead.outreach_text,
        )
        lead.status = "contacted"
        db.commit()
        return RedirectResponse(
            f"/leads/{lead_id}?message={quote_plus('E-mail is verzonden via Zoho Mail.')}",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            f"/leads/{lead_id}?error=1&message={quote_plus('Verzenden mislukt: ' + str(exc))}",
            status_code=303,
        )



@app.post("/leads/{lead_id}/regenerate-outreach", dependencies=[Depends(auth)])
def regenerate_lead_outreach(lead_id: int, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404)
    analysis = {
        "industry": lead.industry,
        "company_summary": lead.company_summary,
        "employee_signal": lead.employee_signal,
        "vacancies_signal": lead.vacancies_signal,
        "employer_branding_signal": lead.employer_branding_signal,
        "event_signal": lead.event_signal,
        "growth_signal": lead.growth_signal,
        "merch_signal": lead.merch_signal,
        "recommended_offer": "Custom kleding & merchandise in eigen huisstijl",
        "lead_reason": lead.lead_reason,
    }
    lead.recommended_offer = "Custom kleding & merchandise in eigen huisstijl"
    lead.outreach_text = generate_outreach(lead.company_name, lead.contact_name, analysis)
    db.commit()
    return RedirectResponse(
        f"/leads/{lead_id}?message={quote_plus('Nieuw outreachconcept gemaakt.')}",
        status_code=303,
    )
