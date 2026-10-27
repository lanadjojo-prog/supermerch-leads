"""Authenticated Instagram prospect dashboard. No public unauthenticated endpoints."""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import require_basic_auth
from .database import SessionLocal
from .models import InstagramLike, InstagramProspect, Lead
from .services.instagram_campaign import run_instagram_cycle, stats

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/instagram", tags=["instagram"], dependencies=[Depends(require_basic_auth)])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("", response_class=HTMLResponse)
def page(request: Request, db: Session = Depends(_get_db)):
    prospects = db.execute(
        select(InstagramProspect, Lead)
        .join(Lead, InstagramProspect.lead_id == Lead.id)
        .order_by(Lead.score.desc())
        .limit(100)
    ).all()
    likes = db.execute(
        select(InstagramLike, InstagramProspect)
        .join(InstagramProspect, InstagramLike.prospect_id == InstagramProspect.id)
        .order_by(InstagramLike.created_at.desc())
        .limit(30)
    ).all()
    return templates.TemplateResponse(
        request, "instagram.html",
        {"status": stats(db), "prospects": prospects, "likes": likes, "app_name": "SuperMerch Instagram"},
    )


def _start_round():
    db = SessionLocal()
    try:
        run_instagram_cycle(db, force=True)
    except Exception:
        logger.exception("Handmatige Instagram ronde mislukt")
    finally:
        db.close()


@router.post("/scan")
def scan(background_tasks: BackgroundTasks):
    background_tasks.add_task(_start_round)
    return RedirectResponse("/instagram?started=1", status_code=303)
