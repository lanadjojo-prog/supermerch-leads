from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..database import SessionLocal
from ..models import ChatCommand
from .daily_engine import run_daily_lead_engine


logger = logging.getLogger(__name__)


def _claim_hour_slot(db, slot_id: str) -> ChatCommand | None:
    if db.scalar(select(ChatCommand.id).where(ChatCommand.command_id == slot_id)):
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


def run_automation_scheduler(stop_event: threading.Event) -> None:
    logger.info(
        "Daily lead scheduler started (%s-%s Europe/Amsterdam)",
        settings.automation_start_hour,
        settings.automation_end_hour,
    )

    while not stop_event.is_set():
        now_local = datetime.now(timezone.utc).astimezone(settings.local_timezone)

        if (
            settings.automation_enabled
            and settings.automation_start_hour <= now_local.hour < settings.automation_end_hour
        ):
            slot_id = f"daily-auto-{now_local.date().isoformat()}-{now_local.hour:02d}"
            db = SessionLocal()
            try:
                row = _claim_hour_slot(db, slot_id)
                if row is not None:
                    try:
                        result = run_daily_lead_engine(db)
                        row = db.get(ChatCommand, row.id)
                        row.status = "complete"
                        row.result = json.dumps(result, ensure_ascii=False)
                        row.finished_at = datetime.utcnow()
                        db.commit()
                        logger.info("Daily automation slot %s complete: %s", slot_id, result)
                    except Exception as exc:
                        logger.exception("Daily automation slot %s failed", slot_id)
                        db.rollback()
                        row = db.get(ChatCommand, row.id)
                        if row is not None:
                            row.status = "error"
                            row.result = str(exc)[:3000]
                            row.finished_at = datetime.utcnow()
                            db.commit()
            finally:
                db.close()

        stop_event.wait(60)
