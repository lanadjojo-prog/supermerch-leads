"""Conservative Instagram prospect research + opt-in engagement for SuperMerch.

Discovery uses only public Instagram profile links on the company's own site.
No authenticated scraping, follow bots, automatic DMs, or guessed usernames.
"""
from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ChatCommand, InstagramLike, InstagramProspect, InstagramScanAttempt, Lead

logger = logging.getLogger(__name__)
GRAPH_ROOT = "https://graph.facebook.com"
USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")
SKIP_PATHS = {"p", "reel", "reels", "stories", "explore", "accounts", "direct", "about", "developer", "legal"}
SENSITIVE_TERMS = ("overleden", "condoleance", "rouw", "uitvaart", "herdenking", "sterfgeval")


class InstagramAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Instagram API HTTP {status}: {message[:200]}")
        self.status = status


def username_from_url(url: str) -> str | None:
    """Accept a profile link only; reject posts, search pages and non-IG hosts."""
    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
        host = (parsed.hostname or "").lower()
        if host not in {"instagram.com", "www.instagram.com"}:
            return None
        segments = [s for s in parsed.path.split("/") if s]
        if len(segments) != 1:
            return None
        name = segments[0]
        if name.lower() in SKIP_PATHS or not USERNAME.fullmatch(name):
            return None
        return name.lower()
    except (TypeError, ValueError):
        return None


def username_from_company_homepage(html: str) -> str | None:
    soup = BeautifulSoup(html[:450000], "html.parser")
    for anchor in soup.select("a[href]"):
        name = username_from_url(anchor.get("href", ""))
        if name:
            return name
    return None


def _fetch_public_handle(url: str) -> str | None:
    if not url.startswith(("http://", "https://")):
        return None
    # Never follow redirects to a different host or non-HTTP protocol.
    origin = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if not origin or origin == "localhost" or "." not in origin:
        return None
    try:
        with httpx.Client(timeout=7, follow_redirects=False) as client:
            current = url
            for _ in range(3):
                resp = client.get(
                    current, headers={"User-Agent": "SuperMerch-LeadResearch/1.0 (+https://supermerch.nl)"}
                )
                if resp.status_code in (301, 302, 303, 307, 308):
                    next_url = urljoin(current, resp.headers.get("location", ""))
                    next_host = (urlparse(next_url).hostname or "").lower().removeprefix("www.")
                    if urlparse(next_url).scheme not in ("https", "http") or next_host != origin:
                        return None
                    current = next_url
                    continue
                if resp.status_code != 200 or "text/html" not in resp.headers.get("content-type", ""):
                    return None
                return username_from_company_homepage(resp.text)
    except httpx.HTTPError:
        return None
    return None


def discover_prospects(db: Session, limit: int = 12) -> dict:
    """Reuse already qualified B2B leads; scan their public homepages once."""
    leads = db.scalars(
        select(Lead)
        .outerjoin(InstagramProspect, InstagramProspect.lead_id == Lead.id)
        .outerjoin(InstagramScanAttempt, InstagramScanAttempt.lead_id == Lead.id)
        .where(
            InstagramProspect.id.is_(None),
            (InstagramScanAttempt.id.is_(None) | (InstagramScanAttempt.checked_at < datetime.utcnow() - timedelta(days=30))),
            Lead.score >= 50,
            Lead.status.in_(("ready_for_review", "approved", "contacted", "possible")),
        )
        .order_by(Lead.score.desc(), Lead.id.desc())
        .limit(max(1, min(limit, 40)))
    ).all()
    found = 0
    for lead in leads:
        name = _fetch_public_handle(lead.website)
        attempt = db.scalar(select(InstagramScanAttempt).where(InstagramScanAttempt.lead_id == lead.id))
        if attempt is None:
            attempt = InstagramScanAttempt(lead_id=lead.id)
            db.add(attempt)
        attempt.checked_at = datetime.utcnow()
        attempt.found = int(bool(name))
        if name:
            db.add(InstagramProspect(
                lead_id=lead.id,
                username=name,
                profile_url=f"https://www.instagram.com/{name}/",
                status="discovered",
            ))
        try:
            db.commit()
            found += int(bool(name))
        except IntegrityError:
            db.rollback()
    return {"checked": len(leads), "found": found}


def _request(method: str, path: str, *, params: dict | None = None, data: dict | None = None) -> dict:
    if not settings.instagram_access_token or not settings.instagram_ig_user_id:
        raise InstagramAPIError(401, "Meta token / IG User ID ontbreekt")
    url = f"{GRAPH_ROOT}/{settings.instagram_graph_version}/{path.lstrip('/')}"
    try:
        with httpx.Client(timeout=20) as client:
            response = client.request(
                method,
                url,
                params=params,
                data=data,
                headers={"Authorization": f"Bearer {settings.instagram_access_token}"},
            )
    except httpx.HTTPError as exc:
        raise InstagramAPIError(503, type(exc).__name__) from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("error", {}).get("message", "Instagram API geweigerd")
        except ValueError:
            detail = "Instagram API geweigerd"
        raise InstagramAPIError(response.status_code, str(detail))
    try:
        return response.json()
    except ValueError as exc:
        raise InstagramAPIError(502, "ongeldige JSON van Meta") from exc


def fetch_business_media(username: str) -> list[dict]:
    """Business Discovery returns media for public Business/Creator accounts only."""
    if not USERNAME.fullmatch(username):
        return []
    fields = (
        f"business_discovery.username({username})"
        "{username,media.limit(12){id,caption,permalink,timestamp,media_type}}"
    )
    data = _request("GET", settings.instagram_ig_user_id, params={"fields": fields})
    discovery = data.get("business_discovery") or {}
    return (discovery.get("media") or {}).get("data") or []


def _is_suitable(media: dict, now: datetime) -> bool:
    if not media.get("id") or not media.get("permalink"):
        return False
    if media.get("media_type") not in ("IMAGE", "CAROUSEL_ALBUM", "VIDEO"):
        return False
    if any(s in str(media.get("caption", "")).lower() for s in SENSITIVE_TERMS):
        return False
    try:
        published = datetime.fromisoformat(str(media.get("timestamp", "")).replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        return now - timedelta(days=90) <= published <= now
    except (TypeError, ValueError):
        return False


def _today_utc_start() -> datetime:
    now = datetime.now(timezone.utc).astimezone(settings.local_timezone)
    local_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).replace(tzinfo=None)


def _today_likes(db: Session) -> list[InstagramLike]:
    return db.scalars(
        select(InstagramLike).where(
            InstagramLike.status == "liked",
            InstagramLike.created_at >= _today_utc_start(),
        )
    ).all()


def like_media(media_id: str) -> dict:
    """Requires reviewed instagram_manage_engagement scope for the connected account."""
    return _request(
        "POST",
        f"{settings.instagram_ig_user_id}/likes",
        data={"media_id": str(media_id)},
    )


def perform_likes(db: Session) -> dict:
    """Hard ceiling: 10/day total, 2/day/profile, never retry already-attempted media."""
    if not settings.instagram_likes_enabled:
        return {"status": "dry_run", "liked": 0, "reason": "INSTAGRAM_LIKES_ENABLED=false"}
    if not settings.instagram_access_token or not settings.instagram_ig_user_id:
        return {"status": "not_connected", "liked": 0}

    today = _today_likes(db)
    remaining = max(0, 10 - len(today))
    if remaining == 0:
        return {"status": "daily_limit", "liked": 0}

    per_profile = {}
    for entry in today:
        per_profile[entry.prospect_id] = per_profile.get(entry.prospect_id, 0) + 1

    prospects = db.scalars(
        select(InstagramProspect)
        .join(Lead, InstagramProspect.lead_id == Lead.id)
        .where(InstagramProspect.status.in_(("discovered", "active")))
        .order_by(Lead.score.desc(), InstagramProspect.id.asc())
        .limit(30)
    ).all()
    liked = skipped = 0
    for prospect in prospects:
        available = min(2 - per_profile.get(prospect.id, 0), remaining)
        if available <= 0:
            continue
        try:
            media_list = fetch_business_media(prospect.username)
        except InstagramAPIError as exc:
            logger.warning("Instagram Business Discovery failed for %s (HTTP %s)", prospect.username, exc.status)
            if exc.status in (401, 403, 429, 503):
                return {"status": "api_paused", "liked": liked, "error": str(exc)}
            prospect.status = "unsupported"
            db.commit()
            continue
        now = datetime.now(timezone.utc)
        suitable = [m for m in media_list if _is_suitable(m, now)]
        random.shuffle(suitable)
        for media in suitable:
            if available == 0:
                break
            media_id = str(media["id"])
            if db.scalar(select(InstagramLike.id).where(InstagramLike.media_id == media_id)):
                skipped += 1
                continue
            log = InstagramLike(
                prospect_id=prospect.id,
                media_id=media_id,
                media_url=str(media["permalink"])[:1000],
                status="pending",
            )
            db.add(log)
            try:
                db.commit()  # reserve unique id before sending, even across workers
            except IntegrityError:
                db.rollback()
                continue
            try:
                like_media(media_id)
            except InstagramAPIError as exc:
                log.status = "failed"
                log.error = str(exc)[:240]
                db.commit()
                if exc.status in (401, 403, 429, 503):
                    return {"status": "api_paused", "liked": liked, "error": str(exc)}
                continue
            log.status = "liked"
            prospect.status = "active"
            db.commit()
            liked += 1
            remaining -= 1
            available -= 1
            per_profile[prospect.id] = per_profile.get(prospect.id, 0) + 1
            if remaining <= 0:
                break
        if remaining <= 0:
            break
    return {"status": "complete", "liked": liked, "already_attempted": skipped}


def run_instagram_cycle(db: Session, *, force: bool = False) -> dict:
    """Scheduled daily after 09:00 Amsterdam, or explicitly requested scan."""
    if not force and not settings.instagram_scan_enabled:
        return {"status": "disabled"}
    today = datetime.now(timezone.utc).astimezone(settings.local_timezone)
    if not force and today.hour < 9:
        return {"status": "outside_window"}
    slot = None
    if not force:
        slot = ChatCommand(
            command_id=f"instagram-daily-{today.date().isoformat()}",
            action="instagram_daily",
            status="running",
        )
        db.add(slot)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return {"status": "already_ran_today"}
    try:
        discovery = discover_prospects(db)
        engagement = perform_likes(db)
        result = {"status": "complete", "discovery": discovery, "engagement": engagement}
        if slot:
            slot.status = "complete"
            slot.result = str(result)[:2000]
            slot.finished_at = datetime.utcnow()
            db.commit()
        return result
    except Exception as exc:
        logger.exception("Instagram daily cycle failed")
        db.rollback()
        if slot:
            row = db.get(ChatCommand, slot.id)
            if row is not None:
                row.status = "error"
                row.result = type(exc).__name__
                row.finished_at = datetime.utcnow()
                db.commit()
        raise


def run_instagram_if_due_background() -> None:
    """Invoked by existing Render health ping; no extra cron or worker required."""
    if not settings.instagram_scan_enabled:
        return
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        run_instagram_cycle(db)
    except Exception:
        logger.exception("Instagram health-triggered cycle error")
    finally:
        db.close()


def stats(db: Session) -> dict:
    return {
        "scan_enabled": settings.instagram_scan_enabled,
        "likes_enabled": settings.instagram_likes_enabled,
        "connected": bool(settings.instagram_access_token and settings.instagram_ig_user_id),
        "prospects": db.scalar(select(func.count(InstagramProspect.id))) or 0,
        "likes_today": len(_today_likes(db)),
        "daily_like_limit": 10,
    }
