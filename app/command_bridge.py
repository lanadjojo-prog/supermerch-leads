from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, update

from .database import SessionLocal
from .models import Campaign, ChatCommand, Lead
from .services import zoho_mail
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
        if db.scalar(select(ChatCommand).where(ChatCommand.command_id == command_id)):
            logger.info("Chat command %s already processed", command_id)
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
            for lead in leads:
                if not lead.email or not lead.outreach_text:
                    failed += 1
                    continue
                try:
                    zoho_mail.send_email(
                        db,
                        to_address=lead.email,
                        subject=f"Merchandise voor {lead.company_name}",
                        content=lead.outreach_text,
                    )
                    lead.status = "contacted"
                    db.commit()
                    sent += 1
                except Exception:
                    db.rollback()
                    failed += 1
            row.result = json.dumps(
                {"sent": sent, "failed": failed, "campaign_id": campaign_id},
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
