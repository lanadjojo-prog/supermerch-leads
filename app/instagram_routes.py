from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from .auth import require_basic_auth
from .config import settings
from .database import SessionLocal
from .main_helpers import templates
from .models import ChatCommand, InstagramAction, InstagramProspect
from .services.instagram_engine import enabled, connection_ready, paused

router = APIRouter(prefix="/instagram", tags=["instagram"])

@router.get("/", dependencies=[Depends(require_basic_auth)])
def dashboard(request: Request):
    with SessionLocal() as db:
        return templates.TemplateResponse(request, "instagram.html", {
            "app_name": settings.app_name,
            "enabled": enabled(), "connected": connection_ready(), "paused": paused(db),
            "prospects": db.scalars(select(InstagramProspect).order_by(InstagramProspect.id.desc()).limit(100)).all(),
            "actions": db.scalars(select(InstagramAction).order_by(InstagramAction.id.desc()).limit(100)).all(),
            "runs": db.scalars(select(ChatCommand).where(ChatCommand.action == "instagram_run").order_by(ChatCommand.id.desc()).limit(10)).all(),
        })
