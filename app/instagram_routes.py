"""Private Instagram prospect dashboard and mailing-list upload."""
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from .auth import require_basic_auth
from .config import settings
from .database import SessionLocal
from .main_helpers import templates
from .models import ChatCommand, InstagramAction, InstagramProspect
from .services.instagram_engine import (
    DAILY_ACCOUNTS_LIMIT, DAILY_LIMIT, PER_ACCOUNT,
    connection_ready, discover, enabled, paused,
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
        return templates.TemplateResponse(request, "instagram.html", {
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
