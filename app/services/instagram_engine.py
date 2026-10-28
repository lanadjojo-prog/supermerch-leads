"""Bounded prospect discovery and official Meta API engagement.

No browser cookies, private endpoints, DMs or automatic follows are used.
A failed/ambiguous API request pauses all future engagement until an operator
resolves the error and explicitly clears the pause in the database.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..database import SessionLocal
from ..models import ChatCommand, InstagramAction, InstagramProspect, Lead
from .crawler import crawl_website

DAILY_LIMIT = 10
PER_ACCOUNT = 2


def enabled():
    return os.getenv("INSTAGRAM_CRAWLER_ENABLED", "false").lower() == "true"


def connection_ready():
    return bool(os.getenv("INSTAGRAM_ACCESS_TOKEN") and
                re.fullmatch(r"\d+", os.getenv("INSTAGRAM_USER_ID", "")) and
                re.fullmatch(r"v\d+\.\d+", os.getenv("INSTAGRAM_GRAPH_VERSION", "")))


def claim(db, key, action):
    """Unique durable claim; never reclaim uncertain work after a crash."""
    row = ChatCommand(command_id=key, action=action, status="running")
    db.add(row)
    try:
        db.commit()
        return row
    except IntegrityError:
        db.rollback()
        return None


def pause(db, reason):
    row = claim(db, "instagram-engagement-paused", "instagram_pause")
    if row:
        row.status = "paused"
        row.result = reason
        db.commit()


def paused(db):
    return db.scalar(select(ChatCommand).where(
        ChatCommand.command_id == "instagram-engagement-paused")) is not None


class MetaError(RuntimeError):
    pass


class MetaClient:
    def __init__(self):
        self.user = os.environ["INSTAGRAM_USER_ID"]
        self.client = httpx.Client(
            base_url=f"https://graph.facebook.com/{os.environ['INSTAGRAM_GRAPH_VERSION']}/",
            headers={"Authorization": f"Bearer {os.environ['INSTAGRAM_ACCESS_TOKEN']}"},
            timeout=20, follow_redirects=False)

    def request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise MetaError("Meta-verzoek niet bevestigd; controleer de verbinding.") from None
        if response.status_code >= 400 or not isinstance(data, dict) or data.get("error"):
            # Do not log raw API responses or request URLs (may contain secrets).
            error = data.get("error", {}) if isinstance(data, dict) else {}
            code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
            raise MetaError(f"Meta API geweigerd (HTTP {response.status_code}, code {code}).")
        return data

    def verify(self):
        data = self.request("GET", self.user, params={"fields": "id,username"})
        if data.get("username", "").lower() != "supermerch.nl":
            raise MetaError("De Meta-koppeling hoort niet bij supermerch.nl.")

    def media(self, username):
        fields = (f"business_discovery.username({username})"
                  "{username,media.limit(10){id,permalink,timestamp}}")
        data = self.request("GET", self.user, params={"fields": fields})
        profile = data.get("business_discovery") or {}
        if profile.get("username", "").lower() != username:
            return []
        return (profile.get("media") or {}).get("data", [])

    def like(self, media_id):
        data = self.request("POST", f"{self.user}/likes", data={"media_id": media_id})
        if data.get("success") is not True:
            raise MetaError("Like niet bevestigd door Meta.")

    def close(self):
        self.client.close()


def discover(db):
    scanned = found = 0
    # Only prospects already qualified for SuperMerch by the existing engine.
    leads = db.scalars(select(Lead).where(
        Lead.score >= 60,
        Lead.status.in_(["ready_for_review", "approved", "drafted", "sent", "contacted", "possible"])
    ).order_by(Lead.score.desc(), Lead.id)).all()
    for lead in leads:
        if scanned >= 5:
            break
        row = claim(db, f"instagram-website-{lead.id}", "instagram_discovery")
        if row is None:
            continue
        scanned += 1
        result = crawl_website(lead.website)
        for username, source in result.instagram_profiles:
            if username == "supermerch.nl" or db.scalar(select(InstagramProspect.id).where(
                    InstagramProspect.username == username)):
                continue
            db.add(InstagramProspect(username=username, lead_id=lead.id, source_url=source))
            try:
                db.commit()
                found += 1
            except IntegrityError:
                db.rollback()
        row.status = "complete" if result.pages else "crawl_failed"
        row.finished_at = datetime.utcnow()
        db.commit()
    return {"websites_scanned": scanned, "accounts_found": found}


def reserve_like(db, username, media, day):
    if db.scalar(select(InstagramAction.id).where(InstagramAction.media_id == str(media["id"]))):
        return None
    for slot in range(DAILY_LIMIT):
        action = InstagramAction(media_id=str(media["id"]), username=username,
            permalink=media["permalink"], day_slot=f"{day}:{slot}")
        db.add(action)
        try:
            db.commit()
            return action
        except IntegrityError:
            db.rollback()
            if db.scalar(select(InstagramAction.id).where(InstagramAction.media_id == str(media["id"]))):
                return None
    return None


def engage(db, client, now):
    if paused(db):
        return {"status": "paused", "likes": 0}
    day = now.astimezone(settings.local_timezone).date().isoformat()
    used = db.scalar(select(func.count(InstagramAction.id)).where(InstagramAction.day_slot.like(f"{day}:%"))) or 0
    if used >= DAILY_LIMIT:
        return {"status": "daily_limit", "likes": 0}
    count = 0
    try:
        client.verify()
        prospects = db.scalars(select(InstagramProspect).where(
            InstagramProspect.status == "discovered").order_by(InstagramProspect.id).limit(1)).all()
        for prospect in prospects:
            # Claim the account permanently before external work: no repeat bursts.
            if not claim(db, f"instagram-account-{prospect.username}", "instagram_engagement"):
                continue
            prospect.status = "processing"
            db.commit()
            for media in client.media(prospect.username):
                if count >= PER_ACCOUNT:
                    break
                try:
                    posted = datetime.fromisoformat(media["timestamp"].replace("Z", "+00:00"))
                    if not posted.tzinfo or not now - timedelta(days=30) <= posted <= now:
                        continue
                    if not re.fullmatch(r"\d+", str(media["id"])):
                        continue
                    if not re.fullmatch(r"https://www\.instagram\.com/(?:p|reel)/[A-Za-z0-9_-]+/?", media["permalink"]):
                        continue
                except (KeyError, ValueError, TypeError):
                    continue
                action = reserve_like(db, prospect.username, media, now.astimezone(settings.local_timezone).date().isoformat())
                if action is None:
                    continue
                try:
                    client.like(action.media_id)
                except Exception:
                    action.status = "unconfirmed"
                    db.commit()
                    raise
                action.status = "liked"
                count += 1
                db.commit()
            prospect.status = "processed"
            db.commit()
        return {"status": "complete", "likes": count}
    except Exception as exc:
        db.rollback()
        reason = str(exc) if isinstance(exc, MetaError) else "Onverwachte fout; controleer de uitvoering."
        pause(db, reason)
        return {"status": "paused", "likes": count, "reason": reason}


def run_background():
    if not enabled():
        return
    now = datetime.now(timezone.utc)
    local = now.astimezone(settings.local_timezone)
    if not 9 <= local.hour < 19:
        return
    engagement_enabled = (connection_ready() and
        os.getenv("INSTAGRAM_LIKES_ENABLED", "false").lower() == "true")
    phase = "engagement" if engagement_enabled else "discovery"
    with SessionLocal() as db:
        # Connecting Meta must be able to start within an hour in which a
        # discovery-only pass already completed. Account/media claims and
        # daily reservations still prevent duplicate engagement.
        slot = claim(db, f"instagram-hour-{local:%Y-%m-%d-%H}-{phase}", "instagram_run")
        if slot is None:
            return
        try:
            result = discover(db)
            if engagement_enabled:
                client = MetaClient()
                try:
                    result.update(engage(db, client, now))
                finally:
                    client.close()
            else:
                result["status"] = "discovery_only_meta_connection_required"
            slot.status = "complete"
            slot.result = json.dumps(result)
        except Exception:
            db.rollback()
            slot.status = "error"
            slot.result = "Instagram-run mislukt; controleer de uitvoering."
        slot.finished_at = datetime.utcnow()
        db.commit()


def status_summary():
    with SessionLocal() as db:
        latest = db.scalar(select(ChatCommand).where(ChatCommand.action == "instagram_run").order_by(ChatCommand.id.desc()).limit(1))
        return {
            "discovery_enabled": enabled(),
            "meta_configured": connection_ready(),
            "likes_enabled": os.getenv("INSTAGRAM_LIKES_ENABLED", "false").lower() == "true",
            "paused": paused(db),
            "accounts_found": db.scalar(select(func.count(InstagramProspect.id))) or 0,
            "confirmed_likes": db.scalar(select(func.count(InstagramAction.id)).where(InstagramAction.status == "liked")) or 0,
            "last_run_status": latest.status if latest else "not_started",
        }
