"""Private Instagram prospect dashboard and mailing-list upload."""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from .auth import require_basic_auth
from .config import settings
from .database import SessionLocal
from .main_helpers import templates
from .models import ChatCommand, InstagramAction, InstagramProspect
from .services.instagram_engine import (
    DAILY_ACCOUNTS_LIMIT, DAILY_LIMIT, DAILY_WEBSITE_SCAN_LIMIT, PER_ACCOUNT,
    _today_start_utc, connection_ready, discover, enabled, paused,
)
from .services.instagram_priority import import_mailing_csv, priority_counts

router = APIRouter(prefix="/instagram", tags=["instagram"])
logger = logging.getLogger(__name__)


@router.get("/", dependencies=[Depends(require_basic_auth)])
def dashboard(request: Request):
    with SessionLocal() as db:
        priority = db.scalars(select(ChatCommand).where(
            ChatCommand.action == "instagram_priority").order_by(ChatCommand.id.desc()).limit(80)).all()
        queue = []
        for entry in priority:
            try:
                data = json.loads(entry.result or "{}")
            except (ValueError, TypeError):
                data = {}
            queue.append({"company": data.get("company_name") or data.get("domain"),
                          "domain": data.get("domain", ""),
                          "username": data.get("username", ""),
                          "status": entry.status})
        local = datetime.now(timezone.utc).astimezone(settings.local_timezone)
        day = local.date().isoformat()
        daily_start = local.replace(hour=9, minute=0, second=0, microsecond=0)
        if 9 <= local.hour < 19:
            daily_start = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        elif local.hour >= 19:
            daily_start += timedelta(days=1)
        scanned_today = db.scalar(select(func.count(ChatCommand.id)).where(
            ChatCommand.action.in_(("instagram_priority_scan", "instagram_discovery")),
            ChatCommand.created_at >= _today_start_utc()
        )) or 0
        found_today = db.scalar(select(func.count(InstagramProspect.id)).where(
            InstagramProspect.created_at >= _today_start_utc()
        )) or 0
        likes_today = db.scalar(select(func.count(InstagramAction.id)).where(
            InstagramAction.status == "liked",
            InstagramAction.day_slot.like(f"{day}:%")
        )) or 0
        return templates.TemplateResponse(request, "instagram.html", {
            "today_scanned": scanned_today,
            "today_found": found_today,
            "daily_website_scan_limit": DAILY_WEBSITE_SCAN_LIMIT,
            "today_likes": likes_today,
            "next_scheduled": daily_start.strftime("%d-%m-%Y %H:%M"),
            "schedule_start_date": os.getenv("INSTAGRAM_DAILY_NOT_BEFORE", ""),
            "app_name": settings.app_name,
            "enabled": enabled(), "connected": connection_ready(), "paused": paused(db),
            "daily_like_limit": DAILY_LIMIT, "per_account": PER_ACCOUNT,
            "daily_account_limit": DAILY_ACCOUNTS_LIMIT,
            "priority_counts": priority_counts(db), "priority_rows": queue,
            "imported": request.query_params.get("imported"),
            "existing": request.query_params.get("existing"),
            "skipped": request.query_params.get("skipped"),
            "prospects": db.scalars(select(InstagramProspect).order_by(
                InstagramProspect.id.desc()).limit(100)).all(),
            "actions": db.scalars(select(InstagramAction).order_by(
                InstagramAction.id.desc()).limit(100)).all(),
            "runs": db.scalars(select(ChatCommand).where(
                ChatCommand.action == "instagram_run").order_by(
                ChatCommand.id.desc()).limit(10)).all(),
        })


@router.post("/import", dependencies=[Depends(require_basic_auth)])
async def import_csv(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, "Upload een .csv-bestand.")
    content = await file.read(2_000_001)
    if len(content) > 2_000_000:
        raise HTTPException(413, "CSV mag maximaal 2 MB zijn.")
    with SessionLocal() as db:
        try:
            outcome = import_mailing_csv(db, content)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(
        "/instagram/?imported={}&existing={}&skipped={}".format(
            outcome["toegevoegd"], outcome["al_bekend"], outcome["overgeslagen"]),
        status_code=303)


def _manual_discovery():
    with SessionLocal() as db:
        try:
            result = discover(db)
            logger.info("Handmatige Instagram-discovery: %s", result)
        except Exception:
            logger.exception("Handmatige Instagram-discovery mislukt")


@router.post("/scan", dependencies=[Depends(require_basic_auth)])
def scan(background_tasks: BackgroundTasks):
    # Scannen is alleen lezen van publieke bedrijfssites; dit endpoint liked of DM't nooit.
    background_tasks.add_task(_manual_discovery)
    return RedirectResponse("/instagram/?scan=started", status_code=303)


@router.get("/internal/system-map")
def private_system_map_status(request: Request):
    """Read-only metrics for authenticated SuperMerch Operations; no PII or tokens."""
    from datetime import datetime, timezone
    from hmac import compare_digest
    from sqlalchemy import func
    from .models import Lead, MailIntegration
    from .services.outreach_delivery import _today_utc_bounds, sent_today

    expected = __import__("os").environ.get("SYSTEM_MAP_BRIDGE_TOKEN", "")
    supplied = request.headers.get("X-System-Map-Token", "")
    if len(expected) < 40 or not compare_digest(supplied, expected):
        raise HTTPException(401, "Unauthorized")

    from .services.instagram_engine import (
        status_summary, DAILY_LIMIT, DAILY_ACCOUNTS_LIMIT,
    )
    now = datetime.now(timezone.utc)
    today = now.astimezone(settings.local_timezone).date().isoformat()
    start, end = _today_utc_bounds()
    def count(db, model, *conditions):
        return int(db.scalar(select(func.count(model.id)).where(*conditions)) or 0)

    with SessionLocal() as db:
        leads_today = count(db, Lead, Lead.created_at >= start, Lead.created_at < end)
        account_claims = count(db, ChatCommand,
            ChatCommand.action == "instagram_engagement",
            ChatCommand.created_at >= start, ChatCommand.created_at < end)
        instagram_found = count(db, InstagramProspect,
            InstagramProspect.created_at >= start,
            InstagramProspect.created_at < end)
        likes_today = count(db, InstagramAction,
            InstagramAction.status == "liked",
            InstagramAction.day_slot.like(today + ":%"))
        mail_sends = int(sent_today(db))
        mailbox_saved = bool(db.scalar(select(MailIntegration.id).where(
            MailIntegration.provider == "zoho")))
        insta = status_summary()
        automation = bool(settings.automation_enabled)
        insta_enabled = bool(insta.get("discovery_enabled"))
        meta_connected = bool(insta.get("meta_configured"))
        likes_enabled = bool(insta.get("likes_enabled"))
        insta_paused = bool(insta.get("paused"))
        latest_error = insta.get("last_run_status") == "error"
        if not insta_enabled:
            insta_state = "idle"
            insta_msg = "Crawlen bewust uitgeschakeld"
        elif insta_paused or latest_error:
            insta_state = "error"
            insta_msg = "Instagram-engagement gepauzeerd of laatste run mislukt"
        elif not meta_connected or not likes_enabled:
            insta_state = "warning"
            insta_msg = "Discovery beschikbaar; Meta-likes niet actief"
        else:
            insta_state = "ok"
            insta_msg = "Instagram-scanner actief; laatste run: " + str(insta.get("last_run_status"))
        lead_state = "ok" if automation else "idle"
        lead_msg = "Dagelijkse leadautomatisering ingeschakeld" if automation else "Automatische lead-outreach staat bewust uit"
        if not mailbox_saved:
            email_state = "warning"
            email_msg = "Geen gekoppelde Zoho-mailbox gevonden"
        elif not automation:
            email_state = "idle"
            email_msg = "Zoho-mailbox opgeslagen; automatische acquisitiemails uit"
        else:
            email_state = "warning"
            email_msg = "Zoho-mailbox opgeslagen; bezorging niet end-to-end getest"
        db_url = str(settings.database_url or "")
        neon = ".neon.tech" in db_url
        return {
            "ok": True,
            "checked_at": now.isoformat(),
            "services": {
                "leads": {"status": lead_state, "message": lead_msg,
                          "daily": {"Nieuwe leads vandaag": leads_today}},
                "instagram": {"status": insta_state, "message": insta_msg,
                              "daily": {"Accounts gevonden": instagram_found,
                                        "Accounts opgepakt": account_claims,
                                        "Likes bevestigd": likes_today}},
                "email": {"status": email_state, "message": email_msg,
                          "daily": {"Verzonden prospectmails": mail_sends}},
                "neon": {"status": "ok" if neon else "warning",
                         "message": "Neon PostgreSQL-query succesvol" if neon
                                    else "Database bereikbaar, provider niet Neon"},
            },
            "tasks": [
                {"name": "Nieuwe leads", "count": leads_today,
                 "target": int(settings.auto_search_target),
                 "description": "Nieuwe leadrecords, vandaag (niet automatisch gekwalificeerd)"},
                {"name": "Instagram-accounts", "count": account_claims,
                 "target": DAILY_ACCOUNTS_LIMIT,
                 "description": "Opgepakte accounts; maximum, geen verplicht dagdoel"},
                {"name": "Instagram-likes", "count": likes_today,
                 "target": DAILY_LIMIT,
                 "description": "Door Meta bevestigde likes; daglimiet"},
                {"name": "E-mails verstuurd", "count": mail_sends,
                 "target": int(settings.daily_send_target),
                 "description": "Bevestigde prospectmails; daglimiet, geen verplicht doel"}
            ],
        }
