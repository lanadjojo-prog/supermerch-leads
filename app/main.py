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
from .tender_routes import router as tender_router
from .command_bridge import process_startup_command
from .config import settings
from .database import Base, SessionLocal, engine
from .models import Campaign, Lead, LeadTrigger, MailIntegration, TenderOpportunity, TenderScanRun
from .services import zoho_mail
from .services.automation_scheduler import run_automation_pass, run_automation_scheduler, run_current_automation_slot
from .services.daily_engine import engine_is_running, run_daily_lead_engine
from .services.discovery import discover_google_places
from .services.intelligence import generate_outreach
from .services.outreach_delivery import DailySendLimitReached, send_lead, sent_today
from .services.pipeline import run_campaign
from .services.tender_radar import run_tender_scan_if_due_background
from . import dropdesk_store

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Keep web-service startup fast so Render can see the listening port.
    # Only schema creation is required synchronously; all nonessential work
    # runs in daemon threads after that.
    Base.metadata.create_all(bind=engine)
    dropdesk_store.init_storage()

    def _startup_housekeeping():
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
        except Exception:
            logger.exception("Startup housekeeping failed")
        finally:
            db.close()

        try:
            process_startup_command()
        except Exception:
            logger.exception("Startup chat command failed")

    threading.Thread(
        target=_startup_housekeeping,
        name="supermerch-startup-housekeeping",
        daemon=True,
    ).start()

    threading.Thread(
        target=run_tender_scan_if_due_background,
        name="supermerch-tender-radar-startup",
        daemon=True,
    ).start()

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
app.include_router(tender_router)
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


def _run_current_automation_background():
    db = SessionLocal()
    try:
        result = run_automation_pass(db)
        logger.warning("Health wake Lead Engine result: %s", result)
    except Exception:
        logger.exception("Health wake Lead Engine failed")
    finally:
        db.close()


@app.get("/health")
def health(background_tasks: BackgroundTasks):
    # Every health hit wakes Render and immediately lets the engines continue.
    background_tasks.add_task(_run_current_automation_background)
    background_tasks.add_task(run_tender_scan_if_due_background)

    db = SessionLocal()
    try:
        latest = db.scalar(select(TenderScanRun).order_by(TenderScanRun.started_at.desc()).limit(1))
        opportunity_count = db.scalar(select(func.count(TenderOpportunity.id))) or 0
        tender = (
            {
                "status": latest.status,
                "fetched": latest.fetched_count,
                "candidates": latest.candidate_count,
                "new": latest.new_count,
                "analyzed": latest.analyzed_count,
                "error": latest.error,
                "started_at": latest.started_at.isoformat() if latest.started_at else None,
                "finished_at": latest.finished_at.isoformat() if latest.finished_at else None,
                "opportunities_total": opportunity_count,
            }
            if latest
            else {"status": "not_started", "opportunities_total": opportunity_count}
        )
    finally:
        db.close()

    return {"ok": True, "automation_triggered": True, "tender_radar": tender}


@app.post("/api/internal/discovery/places")
async def internal_discovery_places(request: Request):
    token = request.headers.get("X-Discovery-Bridge-Token", "")
    if not settings.discovery_bridge_token or not compare_digest(token, settings.discovery_bridge_token):
        raise HTTPException(401, "Unauthorized")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    query = str(payload.get("query") or "").strip()
    region = str(payload.get("region") or "").strip()
    try:
        limit = max(1, min(int(payload.get("limit") or 15), 25))
    except Exception:
        limit = 15

    if not query or not region:
        raise HTTPException(400, "query and region are required")

    try:
        companies = discover_google_places(query, region, limit=limit)
    except Exception as exc:
        logger.exception("Discovery bridge failed for %s / %s", query, region)
        raise HTTPException(502, str(exc))

    return {
        "ok": True,
        "query": query,
        "region": region,
        "companies": [
            {
                "name": c.name,
                "website": c.website,
                "address": c.address,
                "source": c.source,
            }
            for c in companies
        ],
    }


@app.post("/api/internal/dropdesk-storage")
async def internal_dropdesk_storage(request: Request):
    token = request.headers.get("X-Dropdesk-Storage-Key", "")
    if not settings.dropdesk_storage_key or not compare_digest(token, settings.dropdesk_storage_key):
        raise HTTPException(401, "Unauthorized")
    if request.headers.get("content-length"):
        try:
            if int(request.headers["content-length"]) > 2_500_000:
                raise HTTPException(413, "Payload too large")
        except ValueError:
            raise HTTPException(400, "Invalid content length")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    action = str(payload.get("action") or "")
    try:
        if action == "ping":
            return {"ok": dropdesk_store.ping()}
        if action == "get":
            return {"value": dropdesk_store.get_record(payload.get("kind"), payload.get("id"))}
        if action == "all":
            return {"values": dropdesk_store.all_records(payload.get("kind"))}
        if action == "batch":
            return {"ok": True, "count": dropdesk_store.apply_ops(payload.get("ops") or [])}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    raise HTTPException(400, "Unsupported action")


@app.post("/api/internal/daily-lead-engine")
def internal_daily_lead_engine(request: Request, db: Session = Depends(get_db)):
    token = request.headers.get("X-Automation-Token", "")
    if not settings.daily_job_token or not compare_digest(token, settings.daily_job_token):
        raise HTTPException(401, "Unauthorized")
    return run_current_automation_slot(db)


def _run_daily_engine_now_background():
    db = SessionLocal()
    try:
        result = run_daily_lead_engine(db)
        logger.info("Manual Lead Engine run result: %s", result)
    except Exception:
        logger.exception("Manual Lead Engine run failed")
    finally:
        db.close()


@app.post("/automation/run-now", dependencies=[Depends(auth)])
def run_automation_now(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_daily_engine_now_background)
    return RedirectResponse(
        f"/?message={quote_plus('Lead Engine gestart. De run verwerkt leads op de achtergrond.')}",
        status_code=303,
    )


def _automation_status_payload(db: Session) -> dict:
    sent = sent_today(db)
    latest_auto = db.scalar(
        select(Campaign)
        .where(Campaign.name.like("AUTO %"))
        .order_by(Campaign.created_at.desc())
        .limit(1)
    )
    running = engine_is_running()
    from datetime import datetime, timezone
    now_local = datetime.now(timezone.utc).astimezone(settings.local_timezone)
    inside_window = (
        settings.automation_enabled
        and settings.automation_start_hour <= now_local.hour < settings.automation_end_hour
    )
    if sent >= settings.daily_send_target:
        label = "Dagdoel bereikt"
    elif running:
        label = "Engine actief"
    elif inside_window:
        label = "Automatisch actief – volgende run binnen 5 min"
    else:
        label = "Automatisering gepauzeerd buiten het dagvenster"

    return {
        "running": running,
        "label": label,
        "sent_today": sent,
        "daily_target": settings.daily_send_target,
        "remaining": max(0, settings.daily_send_target - sent),
        "campaign": (
            {
                "id": latest_auto.id,
                "name": latest_auto.name,
                "status": latest_auto.status,
                "lead_count": len(latest_auto.leads),
                "last_run_at": latest_auto.last_run_at.isoformat() if latest_auto.last_run_at else None,
            }
            if latest_auto is not None
            else None
        ),
    }


@app.get("/api/automation/status", dependencies=[Depends(auth)])
def automation_status(db: Session = Depends(get_db)):
    return _automation_status_payload(db)


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
def dashboard(request: Request, db: Session = Depends(get_db)):
    campaigns = db.scalars(select(Campaign).order_by(Campaign.created_at.desc())).all()
    counts = dict(db.execute(select(Lead.status, func.count(Lead.id)).group_by(Lead.status)).all())
    trigger_count = db.scalar(select(func.count(LeadTrigger.id))) or 0
    triggered_lead_count = db.scalar(select(func.count(func.distinct(LeadTrigger.lead_id)))) or 0
    top_leads = db.scalars(select(Lead).where(Lead.status == "ready_for_review").order_by(Lead.score.desc(), Lead.created_at.desc()).limit(10)).all()
    status = _automation_status_payload(db)
    return templates.TemplateResponse(request, "dashboard.html", {"campaigns": campaigns, "counts": counts, "top_leads": top_leads, "trigger_count": trigger_count, "triggered_lead_count": triggered_lead_count, "app_name": settings.app_name, "places_ready": bool(settings.google_places_api_key), "ai_ready": bool(settings.openai_api_key), "zoho_connected": zoho_mail.connected(db), "sent_today": status["sent_today"], "daily_target": settings.daily_send_target, "automation_status": status})


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
    triggers = db.scalars(
        select(LeadTrigger)
        .where(LeadTrigger.lead_id == lead.id)
        .order_by(LeadTrigger.strength.desc(), LeadTrigger.created_at.desc())
    ).all()
    return templates.TemplateResponse(request, "lead_detail.html", {"lead": lead, "triggers": triggers, "mailto": mailto, "app_name": settings.app_name, "zoho_connected": zoho_mail.connected(db), "send_message": request.query_params.get("message"), "send_error": request.query_params.get("error") == "1"})


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
    strongest_trigger = db.scalar(
        select(LeadTrigger)
        .where(LeadTrigger.lead_id == lead.id)
        .order_by(LeadTrigger.strength.desc(), LeadTrigger.created_at.desc())
        .limit(1)
    )
    offer = (
        strongest_trigger.recommended_offer
        if strongest_trigger and strongest_trigger.recommended_offer
        else (lead.recommended_offer or "Custom kleding & merchandise in eigen huisstijl")
    )
    analysis = {"industry": lead.industry, "company_summary": lead.company_summary, "employee_signal": lead.employee_signal, "vacancies_signal": lead.vacancies_signal, "employer_branding_signal": lead.employer_branding_signal, "event_signal": lead.event_signal, "growth_signal": lead.growth_signal, "merch_signal": lead.merch_signal, "recommended_offer": offer, "lead_reason": lead.lead_reason}
    if strongest_trigger:
        analysis.update({
            "trigger_label": strongest_trigger.label,
            "trigger_evidence": strongest_trigger.evidence,
            "trigger_source_url": strongest_trigger.source_url,
            "trigger_strength": strongest_trigger.strength,
            "trigger_type": strongest_trigger.trigger_type,
        })
    lead.recommended_offer = offer
    lead.outreach_text = generate_outreach(lead.company_name, lead.contact_name, analysis); db.commit()
    return RedirectResponse(f"/leads/{lead_id}?message={quote_plus('Nieuw outreachconcept gemaakt.')}", status_code=303)
