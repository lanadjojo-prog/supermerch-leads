from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Lead, OutreachSend
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
        select(func.count(OutreachSend.id)).where(
            OutreachSend.sent_at >= start_utc,
            OutreachSend.sent_at < end_utc,
        )
    ) or 0

    # Backward-compatible protection for mails sent before the send-log table existed.
    legacy = db.scalar(
        select(func.count(Lead.id)).where(
            Lead.status == "contacted",
            Lead.updated_at >= start_utc,
            Lead.updated_at < end_utc,
            ~exists(select(OutreachSend.id).where(OutreachSend.lead_id == Lead.id)),
        )
    ) or 0

    return int(logged + legacy)


def remaining_today(db: Session) -> int:
    return max(0, settings.daily_send_target - sent_today(db))


def send_lead(db: Session, lead: Lead) -> bool:
    if db.scalar(select(OutreachSend).where(OutreachSend.lead_id == lead.id)):
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
        OutreachSend(
            lead_id=lead.id,
            provider="zoho",
            recipient=lead.email,
            subject=subject,
            sent_at=datetime.utcnow(),
        )
    )
    lead.status = "contacted"
    db.commit()
    return True
