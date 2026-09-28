from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..database import SessionLocal
from ..models import ChatCommand
from .daily_engine import run_daily_lead_engine


logger = logging.getLogger(__name__)


def _claim_hour_slot(db, slot_id: str) -> ChatCommand | None:
    existing = db.scalar(select(ChatCommand).where(ChatCommand.command_id == slot_id))
    if existing is not None:
        stale_running = (
            existing.status == "running"
            and existing.created_at <= datetime.utcnow() - timedelta(minutes=20)
        )
        if existing.status == "error" or stale_running:
            existing.status = "running"
            existing.result = None
            existing.finished_at = None
            db.commit()
            db.refresh(existing)
            return existing
        return None

    row = ChatCommand(
        command_id=slot_id,
        action="daily_auto",
        status="running",
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(row)
    return row


def run_current_automation_slot(db) -> dict:
    now_local = datetime.now(timezone.utc).astimezone(settings.local_timezone)

    if not settings.automation_enabled:
        return {"ok": True, "status": "automation_disabled"}

    if not (
        settings.automation_start_hour
        <= now_local.hour
        < settings.automation_end_hour
    ):
        return {
            "ok": True,
            "status": "outside_window",
            "local_time": now_local.isoformat(),
        }

    slot_id = f"daily-auto-{now_local.date().isoformat()}-{now_local.hour:02d}"
    row = _claim_hour_slot(db, slot_id)
    if row is None:
        existing = db.scalar(select(ChatCommand).where(ChatCommand.command_id == slot_id))
        return {
            "ok": True,
            "status": "slot_already_claimed",
            "slot_id": slot_id,
            "slot_status": existing.status if existing is not None else "unknown",
        }

    try:
        result = run_daily_lead_engine(db)
        row = db.get(ChatCommand, row.id)
        row.status = "complete"
        row.result = json.dumps(result, ensure_ascii=False)
        row.finished_at = datetime.utcnow()
        db.commit()
        logger.info("Daily automation slot %s complete: %s", slot_id, result)
        return {
            "ok": True,
            "status": "complete",
            "slot_id": slot_id,
            "result": result,
        }
    except Exception as exc:
        logger.exception("Daily automation slot %s failed", slot_id)
        db.rollback()
        row = db.get(ChatCommand, row.id)
        if row is not None:
            row.status = "error"
            row.result = str(exc)[:3000]
            row.finished_at = datetime.utcnow()
            db.commit()
        raise


def run_automation_scheduler(stop_event: threading.Event) -> None:
    """Legacy in-process fallback.

    The production schedule is now driven by GitHub Actions so Render may sleep
    between runs. This function remains available for local/temporary use.
    """
    logger.info(
        "Daily lead scheduler started (%s-%s Europe/Amsterdam)",
        settings.automation_start_hour,
        settings.automation_end_hour,
    )

    while not stop_event.is_set():
        db = SessionLocal()
        try:
            run_current_automation_slot(db)
        except Exception:
            logger.exception("In-process automation tick failed")
        finally:
            db.close()
        stop_event.wait(60)
