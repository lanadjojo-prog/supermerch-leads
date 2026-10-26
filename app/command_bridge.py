from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, update

from .database import SessionLocal
from .models import Campaign, ChatCommand, Lead
from .services import zoho_mail
from .services.outreach_delivery import DailySendLimitReached, send_lead, sent_today
from .services.pipeline import run_campaign

logger = logging.getLogger(__name__)
COMMAND_FILE = Path(__file__).resolve().parent.parent / "chat_command.json"


def _load_command() -> dict | None:
    if not COMMAND_FILE.exists():
        return None
    try:
        data = json.loads(COMMAND_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Could not parse chat_command.json")
        return None
    if not data.get("command_id") or not data.get("action"):
        return None
    return data


def process_startup_command() -> None:
    command = _load_command()
    if not command or command.get("action") == "noop":
        return

    db = SessionLocal()
    row = None
    try:
        command_id = str(command["command_id"])
        existing = db.scalar(select(ChatCommand).where(ChatCommand.command_id == command_id))
        if existing:
            logger.warning(
                "Chat command %s already processed: status=%s result=%s",
                command_id,
                existing.status,
                existing.result,
            )
            return

        row = ChatCommand(command_id=command_id, action=str(command["action"]), status="running")
        db.add(row)
        db.commit()
        db.refresh(row)

        action = command["action"]
        payload = command.get("payload") or {}

        if action == "run_daily_drafts":
            # Explicit one-off recovery command; it is never a send action.
            from .config import settings
            from .services.daily_engine import run_daily_lead_engine
            if settings.automated_outreach_enabled:
                raise RuntimeError("Manual draft recovery refuses automated sending")
            result = run_daily_lead_engine(db)
            row.result = json.dumps(result, ensure_ascii=False)
            if result.get("status") == "discovery_blocked":
                raise RuntimeError(
                    "Draft recovery blocked: " +
                    str(result.get("discovery_error") or "discovery_unavailable")
                )

        elif action == "start_campaign":
            niche = str(payload.get("niche") or "").strip()
            if not niche:
                raise ValueError("niche is required")
            region = str(payload.get("region") or "Nederland").strip()
            search_query = str(payload.get("search_query") or niche).strip()
            target_count = max(1, min(int(payload.get("target_count", 25)), 60))
            campaign = Campaign(
                name=f"{niche} – {region}",
                niche=niche,
                region=region,
                search_query=search_query,
                target_count=target_count,
                status="queued",
            )
            db.add(campaign)
            db.commit()
            db.refresh(campaign)
            result = run_campaign(db, campaign)
            row.result = json.dumps(
                {"campaign_id": campaign.id, "campaign_status": campaign.status, **result},
                ensure_ascii=False,
            )

        elif action == "approve_review":
            campaign_id = payload.get("campaign_id")
            stmt = update(Lead).where(Lead.status == "ready_for_review")
            if campaign_id is not None:
                stmt = stmt.where(Lead.campaign_id == int(campaign_id))
            changed = db.execute(stmt.values(status="approved")).rowcount or 0
            db.commit()
            row.result = json.dumps({"approved": changed, "campaign_id": campaign_id}, ensure_ascii=False)

        elif action == "send_approved":
            campaign_id = payload.get("campaign_id")
            if not zoho_mail.connected(db):
                raise RuntimeError("Zoho Mail is not connected")
            stmt = select(Lead).where(Lead.status == "approved")
            if campaign_id is not None:
                stmt = stmt.where(Lead.campaign_id == int(campaign_id))
            leads = db.scalars(stmt.order_by(Lead.created_at.asc())).all()
            sent = failed = 0
            limit_reached = False
            for lead in leads:
                if not lead.email or not lead.outreach_text:
                    failed += 1
                    continue
                try:
                    if send_lead(db, lead):
                        sent += 1
                except DailySendLimitReached:
                    limit_reached = True
                    break
                except Exception:
                    failed += 1
            row.result = json.dumps(
                {
                    "sent": sent,
                    "failed": failed,
                    "daily_limit_reached": limit_reached,
                    "sent_today": sent_today(db),
                    "campaign_id": campaign_id,
                },
                ensure_ascii=False,
            )
        elif action == "import_researched_drafts":
            # Curated, public B2B contacts: create reviewable lead concepts only.
            # This action NEVER calls a mail transport or approves leads.
            from sqlalchemy import func
            entries = payload.get("prospects") or []
            if not isinstance(entries, list) or not 1 <= len(entries) <= 25:
                raise ValueError("prospects must be a list of 1 to 25 entries")
            name = str(payload.get("campaign_name") or "Research – reviewed concepts")[:160]
            campaign = Campaign(
                name=name,
                niche=str(payload.get("niche") or "B2B gripsokken")[:160],
                region=str(payload.get("region") or "Nederland")[:160],
                search_query="Handmatig geverifieerde openbare zakelijke contactadressen",
                target_count=len(entries),
                status="completed",
                last_run_at=datetime.utcnow(),
            )
            db.add(campaign)
            db.flush()
            created = []
            skipped = []
            for item in entries:
                company = str(item.get("company_name") or "").strip()[:220]
                domain = str(item.get("domain") or "").strip().lower().removeprefix("www.")[:220]
                email = str(item.get("email") or "").strip().lower()[:220]
                website = str(item.get("website") or "").strip()[:500]
                email_source_url = str(item.get("email_source_url") or "").strip()[:500]
                subject = str(item.get("subject") or "").strip()[:220]
                body = str(item.get("body") or "").strip()
                if not all((company, domain, website, email_source_url, subject, body)) or "@" not in email:
                    raise ValueError("Incomplete manually verified prospect")
                known = db.scalar(select(Lead.id).where(
                    (func.lower(Lead.domain) == domain) |
                    (func.lower(Lead.email) == email)
                ).limit(1))
                if known is not None:
                    skipped.append({"company": company, "reason": "existing lead (domain or email)"})
                    continue
                lead = Lead(
                    campaign_id=campaign.id, company_name=company, domain=domain,
                    website=website, email=email, email_source_url=email_source_url,
                    outreach_text=body, source="researched_public_business_contact",
                    status="ready_for_review", score=85,
                    industry="Indoor trampoline- en jumpcentra",
                    recommended_offer="Gripsokken met antislip in eigen design + gratis vrijblijvend ontwerp",
                    lead_reason="Conceptonderwerp: " + subject +
                        " | Inhoud opgesteld voor een vrijblijvend ontwerpvoorstel.",
                    score_reasons="Geselecteerd wegens directe toepassing van gripsokken in indoor leisure.",
                )
                db.add(lead)
                created.append({"company": company, "email": email})
            db.commit()
            row.result = json.dumps(
                {"campaign_id": campaign.id, "created_count": len(created),
                 "skipped_count": len(skipped), "created": created,
                 "skipped": skipped, "zoho_drafts_created": 0,
                 "emails_sent": 0, "status": "review_only"},
                ensure_ascii=False,
            )

        elif action == "save_curated_zoho_drafts":
            # Explicit user-requested mailbox drafts: NEVER send a message.
            # Each item has a durable per-recipient reservation so a restart
            # or uncertain Zoho network response will not duplicate the draft.
            import hashlib
            from sqlalchemy.exc import IntegrityError
            if not zoho_mail.connected(db):
                raise RuntimeError("Zoho Mail is not connected")
            entries = payload.get("drafts") or []
            if not isinstance(entries, list) or not 1 <= len(entries) <= 10:
                raise ValueError("Provide between 1 and 10 curated drafts")
            created, already, failed = [], [], []
            for item in entries:
                recipient = str(item.get("to") or "").strip().lower()
                subject = str(item.get("subject") or "").strip()
                body = str(item.get("content") or "").strip()
                if ("@" not in recipient or any(ch.isspace() or ch in ",;" for ch in recipient)
                    or not subject or not body):
                    failed.append({"to": recipient, "reason": "invalid fields"})
                    continue
                digest = hashlib.sha256((recipient + "\\n" + subject).encode("utf-8")).hexdigest()[:28]
                key = "curated-gripsocks-draft-" + digest
                prior = db.scalar(select(ChatCommand).where(ChatCommand.command_id == key))
                if prior is not None:
                    already.append({"to": recipient, "status": prior.status})
                    continue
                claim = ChatCommand(command_id=key, action="manual_zoho_draft", status="running")
                db.add(claim)
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    already.append({"to": recipient, "status": "claimed_elsewhere"})
                    continue
                try:
                    response = zoho_mail.save_draft(
                        db, to_address=recipient, subject=subject, content=body
                    )
                    data = response.get("data") or {}
                    if not isinstance(data, dict):
                        data = {}
                    claim.status = "complete"
                    claim.finished_at = datetime.utcnow()
                    claim.result = json.dumps({
                        "to": recipient, "subject": subject,
                        "zoho_message_id": str(data.get("messageId") or ""),
                        "sent": False
                    }, ensure_ascii=False)
                    db.commit()
                    created.append({"to": recipient, "subject": subject})
                    logger.warning("Curated Zoho draft saved: recipient=%s sent=false", recipient)
                except Exception as exc:
                    db.rollback()
                    claim = db.scalar(select(ChatCommand).where(ChatCommand.command_id == key))
                    if claim is not None:
                        claim.status = "error"
                        claim.finished_at = datetime.utcnow()
                        claim.result = type(exc).__name__[:90]
                        db.commit()
                    failed.append({"to": recipient, "reason": type(exc).__name__})
                    logger.warning("Curated Zoho draft failed: recipient=%s type=%s sent=false", recipient, type(exc).__name__)
            row.result = json.dumps({
                "created_count": len(created), "already_count": len(already),
                "failed_count": len(failed), "created": created,
                "already": already, "failed": failed,
                "emails_sent": 0
            }, ensure_ascii=False)
            logger.warning(
                "Curated Zoho draft batch finished: created=%d already=%d failed=%d emails_sent=0",
                len(created), len(already), len(failed)
            )

        elif action == "send_direct_emails":
            if not zoho_mail.connected(db):
                raise RuntimeError("Zoho Mail is not connected")
            emails = payload.get("emails") or []
            if not isinstance(emails, list) or not emails:
                raise ValueError("emails is required")
            if len(emails) > 10:
                raise ValueError("maximum 10 direct emails per command")
            sent = []
            failed = []
            for item in emails:
                to_address = str(item.get("to") or "").strip()
                subject = str(item.get("subject") or "").strip()
                content = str(item.get("content") or "").strip()
                if not to_address or "@" not in to_address or not subject or not content:
                    failed.append({"to": to_address, "error": "missing to/subject/content"})
                    continue
                try:
                    zoho_mail.send_email(db, to_address, subject, content)
                    sent.append(to_address)
                except Exception as exc:
                    logger.exception("Direct Zoho send failed for %s: %s", to_address, exc)
                    failed.append({"to": to_address, "error": str(exc)[:300]})
            row.result = json.dumps(
                {"sent": sent, "failed": failed, "count_sent": len(sent), "count_failed": len(failed)},
                ensure_ascii=False,
            )

        else:
            raise ValueError(f"Unsupported action: {action}")

        row.status = "complete"
        row.finished_at = datetime.utcnow()
        db.commit()
        logger.info("Chat command %s completed: %s", command_id, row.result)
    except Exception as exc:
        logger.exception("Chat command failed")
        db.rollback()
        try:
            if row is not None:
                row = db.get(ChatCommand, row.id)
                row.status = "error"
                row.result = str(exc)[:3000]
                row.finished_at = datetime.utcnow()
                db.commit()
        except Exception:
            logger.exception("Could not persist chat command failure")
    finally:
        db.close()
