from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ChatCommand, Lead
from . import zoho_mail


AMSTERDAM_TZ = ZoneInfo("Europe/Amsterdam")


class DailySendLimitReached(RuntimeError):
    pass


def _today_utc_bounds() -> tuple[datetime, datetime]:
    now_local = datetime.now(timezone.utc).astimezone(AMSTERDAM_TZ)
    start_local = datetime.combine(now_local.date(), time.min, tzinfo=AMSTERDAM_TZ)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=None),
        end_local.astimezone(timezone.utc).replace(tzinfo=None),
    )


def sent_today(db: Session) -> int:
    start_utc, end_utc = _today_utc_bounds()

    logged = db.scalar(
        select(func.count(ChatCommand.id)).where(
            ChatCommand.action == "outreach_send",
            ChatCommand.status == "complete",
            ChatCommand.finished_at >= start_utc,
            ChatCommand.finished_at < end_utc,
        )
    ) or 0

    contacted_today = db.scalar(
        select(func.count(Lead.id)).where(
            Lead.status == "contacted",
            Lead.updated_at >= start_utc,
            Lead.updated_at < end_utc,
        )
    ) or 0

    # Contacted rows cover legacy sends; send-log rows make new sends idempotent.
    return int(max(logged, contacted_today))


def remaining_today(db: Session) -> int:
    return max(0, settings.daily_send_target - sent_today(db))


def send_lead(db: Session, lead: Lead) -> bool:
    log_id = f"outreach-send-{lead.id}"
    if db.scalar(select(ChatCommand.id).where(ChatCommand.command_id == log_id)):
        lead.status = "contacted"
        db.commit()
        return False

    if remaining_today(db) <= 0:
        raise DailySendLimitReached(
            f"Daglimiet van {settings.daily_send_target} succesvolle mails is bereikt."
        )

    if not lead.email or not lead.outreach_text:
        raise ValueError("Lead heeft geen verzendklaar e-mailadres en outreachtekst.")

    subject = f"Merchandise voor {lead.company_name}"

    try:
        zoho_mail.send_email(
            db,
            to_address=lead.email,
            subject=subject,
            content=lead.outreach_text,
        )
    except Exception:
        db.rollback()
        failed_lead = db.get(Lead, lead.id)
        if failed_lead:
            failed_lead.status = "send_failed"
            db.commit()
        raise

    db.add(
        ChatCommand(
            command_id=log_id,
            action="outreach_send",
            status="complete",
            result=json.dumps(
                {
                    "lead_id": lead.id,
                    "recipient": lead.email,
                    "subject": subject,
                    "provider": "zoho",
                },
                ensure_ascii=False,
            ),
            finished_at=datetime.utcnow(),
        )
    )
    lead.status = "contacted"
    db.commit()
    return True
