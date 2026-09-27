import logging
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .auth import require_basic_auth
from .database import SessionLocal
from .models import Campaign, Lead
from .services import zoho_mail
from .services.outreach_delivery import DailySendLimitReached, send_lead, sent_today
from .services.pipeline import run_campaign

router = APIRouter(prefix="/api/chat", tags=["chat-actions"])
logger = logging.getLogger(__name__)


def auth(request: Request):
    require_basic_auth(request)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class CampaignStart(BaseModel):
    niche: str = Field(min_length=2, max_length=120)
    region: str = Field(default="Nederland", min_length=2, max_length=120)
    search_query: str | None = Field(default=None, max_length=240)
    target_count: int = Field(default=25, ge=1, le=60)


class CampaignScope(BaseModel):
    campaign_id: int | None = None


def _run_campaign_background(campaign_id: int):
    db = SessionLocal()
    try:
        campaign = db.get(Campaign, campaign_id)
        if campaign:
            run_campaign(db, campaign)
    finally:
        db.close()


@router.get("/status", dependencies=[Depends(auth)])
def chat_status(db: Session = Depends(get_db)):
    counts = dict(db.execute(select(Lead.status, func.count(Lead.id)).group_by(Lead.status)).all())
    latest = db.scalars(select(Campaign).order_by(Campaign.created_at.desc()).limit(5)).all()
    return {
        "lead_counts": counts,
        "zoho_connected": zoho_mail.connected(db),
        "sent_today": sent_today(db),
        "daily_send_target": 30,
        "campaigns": [
            {"id": c.id, "name": c.name, "status": c.status, "target_count": c.target_count}
            for c in latest
        ],
    }


@router.post("/campaigns/start", dependencies=[Depends(auth)])
def chat_start_campaign(payload: CampaignStart, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    niche = payload.niche.strip()
    region = payload.region.strip()
    search_query = (payload.search_query or f"{niche} in {region}").strip()
    campaign = Campaign(
        name=f"{niche} – {region}",
        niche=niche,
        region=region,
        search_query=search_query,
        target_count=payload.target_count,
        status="queued",
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    background_tasks.add_task(_run_campaign_background, campaign.id)
    return {
        "ok": True,
        "campaign_id": campaign.id,
        "name": campaign.name,
        "status": campaign.status,
        "target_count": campaign.target_count,
        "note": "Campaign started. No emails are sent automatically.",
    }


@router.post("/leads/approve-review", dependencies=[Depends(auth)])
def chat_approve_review(payload: CampaignScope, db: Session = Depends(get_db)):
    stmt = select(Lead).where(Lead.status == "ready_for_review")
    if payload.campaign_id is not None:
        if not db.get(Campaign, payload.campaign_id):
            raise HTTPException(404, "Campaign not found")
        stmt = stmt.where(Lead.campaign_id == payload.campaign_id)
    leads = db.scalars(stmt).all()
    approved = needs_attention = 0
    for lead in leads:
        if lead.email and lead.outreach_text:
            lead.status = "approved"
            approved += 1
        else:
            lead.status = "needs_attention"
            needs_attention += 1
    db.commit()
    return {
        "ok": True,
        "approved": approved,
        "needs_attention": needs_attention,
        "campaign_id": payload.campaign_id,
    }


@router.post("/leads/send-approved", dependencies=[Depends(auth)])
def chat_send_approved(payload: CampaignScope, db: Session = Depends(get_db)):
    if not zoho_mail.connected(db):
        raise HTTPException(409, "Zoho Mail is not connected")

    stmt = select(Lead).where(Lead.status == "approved")
    if payload.campaign_id is not None:
        if not db.get(Campaign, payload.campaign_id):
            raise HTTPException(404, "Campaign not found")
        stmt = stmt.where(Lead.campaign_id == payload.campaign_id)

    leads = db.scalars(stmt.order_by(Lead.created_at.asc())).all()
    sent = needs_attention = send_failed = 0
    limit_reached = False

    for lead in leads:
        if not lead.email or not lead.outreach_text:
            lead.status = "needs_attention"
            db.commit()
            needs_attention += 1
            continue
        try:
            if send_lead(db, lead):
                sent += 1
        except DailySendLimitReached:
            limit_reached = True
            break
        except Exception as exc:
            logger.exception("Zoho send failed for lead %s (%s): %s", lead.id, lead.company_name, exc)
            send_failed += 1

    return {
        "ok": send_failed == 0 and needs_attention == 0,
        "sent": sent,
        "needs_attention": needs_attention,
        "send_failed": send_failed,
        "daily_limit_reached": limit_reached,
        "sent_today": sent_today(db),
        "campaign_id": payload.campaign_id,
    }
