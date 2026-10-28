"""Prepare Zoho Mail review-only drafts for new, source-verified SuperMerch prospects.

No outbound email is sent from this module. An explicit Zoho mode=draft API
call is made for each new, qualified, uncontacted lead. Persistent reservations
prevent retry storms and duplicate drafts after restarts.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import func, select, and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Campaign, ChatCommand, Lead
from . import zoho_mail
from .intelligence import outreach_subject, outreach_variant
from .scoring import TARGET_SECTORS

logger = logging.getLogger(__name__)


def _today_utc_bounds() -> tuple[datetime, datetime, str]:
    now = datetime.now(timezone.utc).astimezone(settings.local_timezone)
    begin = datetime.combine(now.date(), time.min, tzinfo=settings.local_timezone)
    end = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=settings.local_timezone)
    return (
        begin.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
        now.date().isoformat(),
    )


def _today_draft_count(db: Session, start: datetime, end: datetime) -> int:
    return int(db.scalar(
        select(func.count(ChatCommand.id)).where(
            ChatCommand.action == "zoho_draft",
            ChatCommand.created_at >= start,
            ChatCommand.created_at < end,
        )
    ) or 0)


def _official_email_source(lead: Lead) -> bool:
    """Only draft if the address was actually found on that business's site."""
    if not lead.email or not lead.email_source_url or not lead.domain:
        return False
    if "@" not in lead.email or any(ch in lead.email for ch in " \t\r\n,;"):
        return False
    origin = (urlparse(lead.email_source_url).hostname or "").lower().removeprefix("www.")
    company = lead.domain.lower().removeprefix("www.")
    return bool(origin and (origin == company or origin.endswith("." + company)))


def _text_for_lead(lead: Lead) -> tuple[str, str]:
    """Use the already researched message; personalize fallback from verified signals."""
    strongest = max(lead.triggers, key=lambda t: t.strength, default=None)
    context = {
        "lead_reason": lead.lead_reason or "",
        "company_summary": lead.company_summary or "",
        "recommended_offer": lead.recommended_offer or "",
        "trigger_label": strongest.label if strongest else "",
        "trigger_evidence": strongest.evidence if strongest else "",
    }
    subject = outreach_subject(lead.company_name, outreach_variant(lead.company_name, context))
    message = (lead.outreach_text or "").strip()
    if not message:
        raise ValueError("Geen geschreven mailtekst voor deze lead.")

    # Previous non-AI template mentioned a 'concrete reason' without saying what
    # it was. Replace that misleading sentence with a verifiable on-site signal.
    generic = (
        f"Ik kwam {lead.company_name} tegen en zag op jullie website een concrete "
        "aanleiding om even contact op te nemen."
    )
    if generic in message:
        label = (strongest.label or "").strip() if strongest else ""
        if label:
            label = label.replace("\n", " ").replace("\r", " ")[:120]
            replacement = (
                f"Ik kwam {lead.company_name} tegen en zag op jullie website "
                f"de vermelding van {label}."
            )
        else:
            replacement = f"Ik kwam {lead.company_name} tegen via jullie website."
        message = message.replace(generic, replacement, 1)
    return subject, message


def prepare_new_zoho_drafts(db: Session, *, per_run: int = 5, daily_limit: int = 30) -> dict:
    """Add no more than per_run drafts and daily_limit attempts for brand-new leads.

    Limits include uncertain API attempts: never retry a possibly-created draft.
    Pre-existing company domains in the database are not used.
    """
    start, end, day = _today_utc_bounds()
    limit = min(30, max(0, daily_limit))
    attempted = _today_draft_count(db, start, end)
    available = max(0, limit - attempted)
    quota = min(max(0, per_run), available)
    result = {
        "date": day, "daily_limit": limit, "already_reserved": attempted,
        "drafts_created": 0, "attempts_failed": 0, "eligible_pending": 0,
        "status": "no_quota" if not quota else "ready",
    }
    if not quota:
        return result
    if not zoho_mail.connected(db):
        result["status"] = "zoho_not_connected"
        return result

    leads = db.scalars(
        select(Lead)
        .join(Campaign)
        .where(
            or_(
                and_(
                    Campaign.name.like(f"AUTO {day} |%"),
                    Campaign.created_at >= start,
                    Campaign.created_at < end,
                    Lead.created_at >= start,
                    Lead.created_at < end,
                ),
                and_(
                    # ChatGPT-sourced verified leads may have been researched
                    # before a mailbox was relinked. Do not strand the backlog.
                    Campaign.name.like("AUTO %"),
                    Lead.source == "chatgpt_verified_site",
                    Lead.created_at >= datetime.utcnow() - timedelta(days=30),
                ),
            ),
            Lead.status.in_(("ready_for_review", "possible")),
            Lead.email.is_not(None),
            Lead.email_source_url.is_not(None),
            Lead.outreach_text.is_not(None),
            Lead.score >= 25,
            Campaign.niche.in_(TARGET_SECTORS),
        )
        .order_by(Lead.score.desc(), Lead.created_at.asc())
    ).all()

    for lead in leads:
        if result["drafts_created"] + result["attempts_failed"] >= quota:
            break
        if not _official_email_source(lead):
            continue
        # These are human-review drafts, not evidence of an active buying request.
        # For fit-only prospects the prepared copy explicitly avoids claiming
        # intent, a dated event or an earlier relationship. Sending is disabled.
        if lead.status == "possible" and not lead.outreach_text:
            continue
        # An address can occur on multiple websites. Do not prepare mail for
        # any address already known from another old or new lead.
        previously_known_email = db.scalar(select(Lead.id).where(
            func.lower(Lead.email) == lead.email.lower(),
            Lead.id != lead.id,
        ).limit(1))
        if previously_known_email is not None:
            continue
        key = f"zoho-draft-lead-{lead.id}"
        if db.scalar(select(ChatCommand.id).where(ChatCommand.command_id == key)):
            continue

        result["eligible_pending"] += 1
        try:
            subject, content = _text_for_lead(lead)
        except Exception:
            logger.exception("Cannot draft lead %s due to missing content", lead.id)
            continue

        # This row is intentionally committed *before* contacting Zoho: if a
        # network timeout occurs after Zoho has saved the draft, the next wake
        # does not create it again.
        command = ChatCommand(command_id=key, action="zoho_draft", status="running")
        db.add(command)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            continue

        try:
            response = zoho_mail.save_draft(
                db, to_address=lead.email, subject=subject, content=content
            )
            command.status = "complete"
            command.finished_at = datetime.utcnow()
            payload = response.get("data") or {}
            if not isinstance(payload, dict):
                payload = {}
            command.result = json.dumps({
                "lead_id": lead.id, "company": lead.company_name,
                "recipient": lead.email, "subject": subject,
                "zoho_message_id": str(payload.get("messageId") or ""),
            }, ensure_ascii=False)
            lead.status = "drafted"
            db.commit()
            result["drafts_created"] += 1
        except Exception as exc:
            db.rollback()
            command = db.scalar(select(ChatCommand).where(ChatCommand.command_id == key))
            if command:
                command.status = "error"
                command.finished_at = datetime.utcnow()
                command.result = type(exc).__name__[:80]
                db.commit()
            result["attempts_failed"] += 1
            logger.warning(
                "Zoho draft attempt failed for lead id=%s, type=%s (never retried automatically)",
                lead.id, type(exc).__name__,
            )
    result["status"] = "completed"
    return result
