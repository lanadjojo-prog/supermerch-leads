from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import require_basic_auth
from .config import settings
from .database import Base, SessionLocal, engine
from .models import Campaign, Lead
from .services.pipeline import run_campaign


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def auth(request: Request):
    require_basic_auth(request)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
def dashboard(request: Request, db: Session = Depends(get_db)):
    campaigns = db.scalars(select(Campaign).order_by(Campaign.created_at.desc())).all()
    counts = dict(db.execute(select(Lead.status, func.count(Lead.id)).group_by(Lead.status)).all())
    top_leads = db.scalars(
        select(Lead).where(Lead.status == "ready_for_review")
        .order_by(Lead.score.desc(), Lead.created_at.desc()).limit(10)
    ).all()
    return templates.TemplateResponse(request, "dashboard.html", {
        "campaigns": campaigns, "counts": counts, "top_leads": top_leads,
        "app_name": settings.app_name,
        "places_ready": bool(settings.google_places_api_key),
        "ai_ready": bool(settings.openai_api_key),
    })


@app.get("/campaigns/new", response_class=HTMLResponse, dependencies=[Depends(auth)])
def new_campaign(request: Request):
    return templates.TemplateResponse(request, "campaign_new.html", {"app_name": settings.app_name})


@app.post("/campaigns", dependencies=[Depends(auth)])
def create_campaign(
    niche: str = Form(...),
    region: str = Form("Nederland"),
    search_query: str = Form(...),
    target_count: int = Form(25),
    db: Session = Depends(get_db),
):
    campaign = Campaign(
        name=f"{niche.strip()} – {region.strip()}",
        niche=niche.strip(),
        region=region.strip(),
        search_query=search_query.strip(),
        target_count=max(1, min(target_count, 60)),
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


def _run_campaign_background(campaign_id: int):
    db = SessionLocal()
    try:
        campaign = db.get(Campaign, campaign_id)
        if campaign:
            run_campaign(db, campaign)
    finally:
        db.close()


@app.post("/campaigns/{campaign_id}/run", dependencies=[Depends(auth)])
def start_campaign(campaign_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404)
    if campaign.status == "running":
        return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)
    campaign.status = "queued"
    db.commit()
    background_tasks.add_task(_run_campaign_background, campaign_id)
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@app.get("/campaigns/{campaign_id}", response_class=HTMLResponse, dependencies=[Depends(auth)])
def campaign_detail(request: Request, campaign_id: int, db: Session = Depends(get_db)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404)
    leads = db.scalars(
        select(Lead).where(Lead.campaign_id == campaign_id)
        .order_by(Lead.score.desc(), Lead.created_at.desc())
    ).all()
    return templates.TemplateResponse(request, "campaign_detail.html", {
        "campaign": campaign, "leads": leads, "app_name": settings.app_name,
    })


@app.get("/leads/{lead_id}", response_class=HTMLResponse, dependencies=[Depends(auth)])
def lead_detail(request: Request, lead_id: int, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404)
    mailto = None
    if lead.email and lead.outreach_text:
        mailto = (
            f"mailto:{lead.email}?subject={quote_plus('Idee voor ' + lead.company_name)}"
            f"&body={quote_plus(lead.outreach_text)}"
        )
    return templates.TemplateResponse(request, "lead_detail.html", {
        "lead": lead, "mailto": mailto, "app_name": settings.app_name,
    })


@app.post("/leads/{lead_id}/status", dependencies=[Depends(auth)])
def set_lead_status(lead_id: int, status_value: str = Form(...), db: Session = Depends(get_db)):
    allowed = {"approved", "rejected", "contacted", "ready_for_review", "possible"}
    if status_value not in allowed:
        raise HTTPException(400, "Ongeldige status")
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404)
    lead.status = status_value
    db.commit()
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)
