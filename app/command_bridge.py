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

        if action == "start_campaign":
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
