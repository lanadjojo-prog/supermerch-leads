from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import require_basic_auth
from .config import settings
from .database import SessionLocal
from .models import TenderOpportunity, TenderOutreach, TenderScanRun
from .services.tender_radar import run_tender_scan_background
from .services.tender_outreach import prepare_tender_outreach
from .main_helpers import templates

router = APIRouter(prefix="/tenders", tags=["tenders"])

def auth(request: Request):
    require_basic_auth(request)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _render_tender_dashboard(request: Request, db: Session, status: str | None, *, admin: bool):
    query = select(TenderOpportunity)
    if admin and status:
        query = query.where(TenderOpportunity.status == status)
    opportunities = db.scalars(
        query.order_by(
            TenderOpportunity.ai_score.desc(),
            TenderOpportunity.published_at.desc(),
            TenderOpportunity.created_at.desc(),
        ).limit(250)
    ).all()
    counts = (
        dict(db.execute(
            select(TenderOpportunity.status, func.count(TenderOpportunity.id))
            .group_by(TenderOpportunity.status)
        ).all()) if admin else {}
    )
    latest_run = (
        db.scalar(select(TenderScanRun).order_by(TenderScanRun.started_at.desc()).limit(1))
        if admin else None
    )
    return templates.TemplateResponse(
        request, "tender_dashboard.html",
        {
            "app_name": settings.app_name,
            "opportunities": opportunities,
            "counts": counts,
            "latest_run": latest_run,
            "active_status": status if admin else None,
            "message": request.query_params.get("message") if admin else None,
            "admin": admin,
        },
    )


@router.get("/", response_class=HTMLResponse)
def tender_dashboard(request: Request, db: Session = Depends(get_db)):
    """Public, read-only list. Never fetch or render private outreach fields."""
    return _render_tender_dashboard(request, db, None, admin=False)


@router.get("/admin/", response_class=HTMLResponse, dependencies=[Depends(auth)])
def tender_admin_dashboard(request: Request, status: str | None = None, db: Session = Depends(get_db)):
    return _render_tender_dashboard(request, db, status, admin=True)


@router.post("/scan", dependencies=[Depends(auth)])
def scan_tenders(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_tender_scan_background)
    return RedirectResponse("/tenders/admin/?message=Zoekronde gestart: TenderNed en openbare inkoopvragen buiten TenderNed. Vernieuw deze pagina over een moment.", status_code=303)

def _render_tender_detail(request: Request, opportunity_id: int, db: Session, *, admin: bool):
    opportunity = db.get(TenderOpportunity, opportunity_id)
    if not opportunity:
        raise HTTPException(404)
    outreach = None
    if admin:
        outreach = db.scalar(select(TenderOutreach).where(
            TenderOutreach.opportunity_id == opportunity.id
        ))
        if outreach is None:
            outreach = prepare_tender_outreach(db, opportunity)
    return templates.TemplateResponse(
        request, "tender_detail.html",
        {
            "app_name": settings.app_name,
            "tender": opportunity,
            "outreach": outreach,
            "message": request.query_params.get("message") if admin else None,
            "admin": admin,
        },
    )


@router.get("/admin/{opportunity_id}", response_class=HTMLResponse, dependencies=[Depends(auth)])
def tender_admin_detail(request: Request, opportunity_id: int, db: Session = Depends(get_db)):
    return _render_tender_detail(request, opportunity_id, db, admin=True)


@router.get("/{opportunity_id}", response_class=HTMLResponse)
def tender_detail(request: Request, opportunity_id: int, db: Session = Depends(get_db)):
    """Public opportunity details; no raw source or confidential outreach."""
    return _render_tender_detail(request, opportunity_id, db, admin=False)


@router.post("/{opportunity_id}/status", dependencies=[Depends(auth)])
def tender_status(opportunity_id: int, status_value: str = Form(...), db: Session = Depends(get_db)):
    allowed = {"new", "interesting", "investigate", "apply", "rejected", "archived"}
    if status_value not in allowed:
        raise HTTPException(400, "Ongeldige status")
    opportunity = db.get(TenderOpportunity, opportunity_id)
    if not opportunity:
        raise HTTPException(404)
    opportunity.status = status_value
    db.commit()
    return RedirectResponse(f"/tenders/admin/{opportunity_id}?message=Status bijgewerkt.", status_code=303)


@router.post("/{opportunity_id}/outreach/refresh", dependencies=[Depends(auth)])
def refresh_tender_outreach(opportunity_id: int, db: Session = Depends(get_db)):
    opportunity = db.get(TenderOpportunity, opportunity_id)
    if not opportunity:
        raise HTTPException(404)
    existing = db.scalar(select(TenderOutreach).where(TenderOutreach.opportunity_id == opportunity_id))
    if existing:
        existing.draft_body = None
        db.commit()
    prepare_tender_outreach(db, opportunity)
    return RedirectResponse(f"/tenders/admin/{opportunity_id}?message=Contactgegevens en concept opnieuw opgebouwd.", status_code=303)
