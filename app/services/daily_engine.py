from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..lead_categories import iter_daily_searches
from ..models import Campaign, Lead
from . import zoho_mail
from .outreach_delivery import DailySendLimitReached, remaining_today, send_lead, sent_today
from .pipeline import run_campaign


logger = logging.getLogger(__name__)
_engine_run_lock = threading.Lock()


def _auto_prefix(day_iso: str) -> str:
    return f"AUTO {day_iso} |"


def _send_candidates(
    db: Session,
    *,
    day_iso: str,
    max_successes: int,
) -> tuple[int, int]:
    if max_successes <= 0:
        return 0, 0

    leads = db.scalars(
        select(Lead)
        .join(Campaign)
        .where(
            Campaign.name.like(f"{_auto_prefix(day_iso)}%"),
            Lead.status.in_(("ready_for_review", "approved")),
            Lead.email.is_not(None),
            Lead.outreach_text.is_not(None),
        )
        .order_by(Lead.score.desc(), Lead.created_at.asc())
    ).all()

    sent = failed = 0
    for lead in leads:
        if sent >= max_successes or remaining_today(db) <= 0:
            break
        try:
            if send_lead(db, lead):
                sent += 1
        except DailySendLimitReached:
            break
        except Exception as exc:
            failed += 1
            logger.exception("Automatische Zoho-send mislukt voor lead %s: %s", lead.id, exc)
    return sent, failed


def _run_daily_lead_engine_impl(db: Session) -> dict:
    if not zoho_mail.connected(db):
        raise RuntimeError("Zoho Mail is niet verbonden.")

    now_local = datetime.now(timezone.utc).astimezone(settings.local_timezone)
    day_iso = now_local.date().isoformat()
    already_sent = sent_today(db)
    remaining = max(0, settings.daily_send_target - already_sent)

    logger.warning(
        "Lead Engine run gestart: %s/%s vandaag verzonden",
        already_sent,
        settings.daily_send_target,
    )

    if remaining <= 0:
        return {
            "ok": True,
            "date": day_iso,
            "sent_today": already_sent,
            "daily_target": settings.daily_send_target,
            "sent_this_run": 0,
            "status": "daily_target_reached",
        }

    run_goal = min(remaining, settings.daily_send_batch)
    sent_this_run = failed_sends = searches_run = created = qualified = 0

    sent_now, failed_now = _send_candidates(
        db,
        day_iso=day_iso,
        max_successes=run_goal,
    )
    sent_this_run += sent_now
    failed_sends += failed_now

    if sent_this_run < run_goal:
        for category_name, query, region in iter_daily_searches(now_local.date()):
            if sent_this_run >= run_goal or remaining_today(db) <= 0:
                break
            if searches_run >= settings.auto_searches_per_run:
                break

            campaign_name = (
                f"AUTO {day_iso} | {category_name[:42]} | {region[:24]} | {query[:48]}"
            )
            if db.scalar(select(Campaign.id).where(Campaign.name == campaign_name)):
                continue

            campaign = Campaign(
                name=campaign_name,
                niche=category_name,
                region=region,
                search_query=query,
                target_count=settings.auto_search_target,
                status="queued",
            )
            db.add(campaign)
            db.commit()
            db.refresh(campaign)
            searches_run += 1
            logger.warning(
                "AUTO campagne aangemaakt: id=%s | %s | %s | %s",
                campaign.id,
                category_name,
                query,
                region,
            )

            try:
                result = run_campaign(db, campaign)
            except Exception as exc:
                logger.exception(
                    "Automatische discovery mislukt voor %s / %s: %s",
                    query,
                    region,
                    exc,
                )
                break

            created += int(result.get("created", 0))
            qualified += int(result.get("qualified", 0))

            sent_now, failed_now = _send_candidates(
                db,
                day_iso=day_iso,
                max_successes=run_goal - sent_this_run,
            )
            sent_this_run += sent_now
            failed_sends += failed_now

    total_today = sent_today(db)
    result = {
        "ok": True,
        "date": day_iso,
        "sent_today": total_today,
        "daily_target": settings.daily_send_target,
        "sent_this_run": sent_this_run,
        "remaining_today": max(0, settings.daily_send_target - total_today),
        "searches_run": searches_run,
        "leads_created": created,
        "qualified": qualified,
        "send_failed": failed_sends,
        "status": (
            "daily_target_reached"
            if total_today >= settings.daily_send_target
            else "continue_next_scheduled_run"
        ),
    }
    logger.warning("Lead Engine run afgerond: %s", result)
    return result


def run_daily_lead_engine(db: Session) -> dict:
    if not _engine_run_lock.acquire(blocking=False):
        return {"ok": True, "status": "already_running"}
    try:
        return _run_daily_lead_engine_impl(db)
    finally:
        _engine_run_lock.release()


def engine_is_running() -> bool:
    return _engine_run_lock.locked()
