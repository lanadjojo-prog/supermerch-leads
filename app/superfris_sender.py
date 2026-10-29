"""SuperFris opt-in / existing-customer SMTP sender.

Separate from IMAP draft creation and from SuperMerch / Zoho. Never sends
unsolicited discovered leads. A human must approve EACH recipient with an
auditable legal basis; all attempts are reserved before the network call.
"""
from __future__ import annotations

import html
import imaplib
import logging
import os
import re
import smtplib
import ssl
import threading
import time
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr
from urllib.parse import urlparse

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func, select, text as sql_text, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from .config import settings
from .database import Base, SessionLocal
from .superfris_leads import (
    MAILBOX, SuperfrisDraftAttempt, SuperfrisLead, _identify_draft_folder, _today_bounds
)

logger = logging.getLogger(__name__)
_guard = threading.Lock()
MAX_SENDS_PER_DAY = 30
SEND_INTERVAL_SECONDS = 10
BASIS = {"consent", "existing_customer", "designated_business_offers"}
TRUTH = ("1", "yes", "true", "on")


class SuperfrisSendAuthorization(Base):
    __tablename__ = "superfris_send_authorizations"
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("superfris_leads.id"), primary_key=True
    )
    basis: Mapped[str] = mapped_column(String(42))
    evidence: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(28), default="approved")
    approved_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class SuperfrisSenderControl(Base):
    __tablename__ = "superfris_sender_control"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)


def _enabled() -> bool:
    return os.getenv("SUPERFRIS_SEND_ENABLED", "false").strip().lower() in TRUTH


def _network_allowed() -> bool:
    # Render Free blocks SMTP 465. Do not consume reservations attempting it.
    # Set only after moving the SMTP worker onto a network with outbound 465.
    return os.getenv("SUPERFRIS_SMTP_NETWORK_ALLOWED", "false").strip().lower() in TRUTH


def _paused(db: Session) -> bool:
    row = db.get(SuperfrisSenderControl, 1)
    return bool(row and row.paused)


def set_paused(db: Session, paused: bool) -> None:
    row = db.get(SuperfrisSenderControl, 1)
    if not row:
        row = SuperfrisSenderControl(id=1, paused=paused)
        db.add(row)
    else:
        row.paused = paused
    db.commit()


def approve(db: Session, lead_id: int, basis: str, evidence: str) -> None:
    if basis not in BASIS or len(evidence.strip()) < 16:
        raise ValueError("Kies een rechtsgrond en beschrijf controleerbaar bewijs (minimaal 16 tekens).")
    if basis == "designated_business_offers" and not evidence.strip().startswith("https://"):
        raise ValueError("Voeg bij een expliciet aanbiedingenadres de bron-URL toe.")
    lead = db.get(SuperfrisLead, lead_id)
    if not lead or lead.status != "drafted":
        raise ValueError("Dit concept is niet door de leadmachine opgeslagen.")
    existing = db.get(SuperfrisSendAuthorization, lead_id)
    if existing and existing.status not in ("revoked",):
        raise ValueError("Dit contact is al goedgekeurd of eerder verwerkt.")
    proof = db.scalar(select(SuperfrisDraftAttempt.id).where(
        SuperfrisDraftAttempt.lead_id == lead_id,
        SuperfrisDraftAttempt.status == "saved"
    ))
    if not proof:
        raise ValueError("Geen bevestigde STRATO-conceptopslag.")
    if existing:
        existing.status = "approved"
        existing.basis = basis
        existing.evidence = evidence.strip()[:2000]
        existing.approved_at = datetime.utcnow()
    else:
        db.add(SuperfrisSendAuthorization(
            lead_id=lead_id, status="approved",
            basis=basis, evidence=evidence.strip()[:2000]
        ))
    db.commit()


def revoke(db: Session, lead_id: int) -> None:
    row = db.get(SuperfrisSendAuthorization, lead_id)
    if row and row.status == "approved":
        row.status = "revoked"
        db.commit()


def status(db: Session) -> dict:
    start, end, _ = _today_bounds()
    attempts = int(db.scalar(select(func.count(SuperfrisSendAuthorization.lead_id)).where(
        SuperfrisSendAuthorization.attempted_at >= start,
        SuperfrisSendAuthorization.attempted_at < end
    )) or 0)
    sent = int(db.scalar(select(func.count(SuperfrisSendAuthorization.lead_id)).where(
        SuperfrisSendAuthorization.status == "sent",
        SuperfrisSendAuthorization.sent_at >= start,
        SuperfrisSendAuthorization.sent_at < end
    )) or 0)
    queued = int(db.scalar(select(func.count(SuperfrisSendAuthorization.lead_id)).where(
        SuperfrisSendAuthorization.status == "approved"
    )) or 0)
    return {
        "enabled": _enabled(),
        "smtp_network_allowed": _network_allowed(),
        "paused": _paused(db),
        "interval_seconds": SEND_INTERVAL_SECONDS,
        "max_daily_attempts": MAX_SENDS_PER_DAY,
        "attempts_today": attempts,
        "sent_today": sent,
        "approved_pending": queued,
        "blocked_reason": (
            "sender_disabled" if not _enabled() else
            "manual_pause" if _paused(db) else
            "smtp_465_unavailable_on_render_free" if not _network_allowed() else
            None
        )
    }


def _sent_folder(folders: list[bytes]) -> str | None:
    for raw in folders:
        line = raw.decode("utf-8", "replace")
        matches = re.search(r' "([^"]+)"\s*$', line)
        mailbox = matches.group(1) if matches else line.rsplit(" ", 1)[-1].strip('"')
        flags = line.split(")", 1)[0].lower()
        base = mailbox.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if "\\sent" in flags or base in ("sent", "sent items", "verzonden", "gesendet", "sent messages"):
            if '"' not in mailbox and "\r" not in mailbox:
                return mailbox
    return None


def _read_approved_strato_draft(client, lead: SuperfrisLead):
    code, folders = client.list()
    if code != "OK":
        raise RuntimeError("IMAP_LIST_FAILED")
    draft_folder = _identify_draft_folder(folders or [])
    sent_folder = _sent_folder(folders or [])
    if not draft_folder or not sent_folder:
        raise RuntimeError("DRAFTS_OR_SENT_FOLDER_MISSING")
    if client.select('"' + draft_folder + '"', readonly=False)[0] != "OK":
        raise RuntimeError("IMAP_SELECT_FAILED")
    code, data = client.uid("SEARCH", None, "HEADER", "X-Superfris-Lead-ID", str(lead.id))
    if code != "OK":
        raise RuntimeError("DRAFT_SEARCH_FAILED")
    uids = (data[0] or b"").split()
    if len(uids) != 1:
        raise ValueError("DRAFT_NOT_UNIQUE_OR_MISSING")
    code, raw = client.uid("FETCH", uids[0], "(RFC822)")
    if code != "OK":
        raise RuntimeError("DRAFT_FETCH_FAILED")
    payload = next((part[1] for part in raw or []
                    if isinstance(part, tuple) and isinstance(part[1], bytes)), None)
    if not payload:
        raise RuntimeError("DRAFT_BODY_MISSING")
    msg = BytesParser(policy=policy.default).parsebytes(payload)
    actual_from = parseaddr(msg.get("From", ""))[1].lower()
    actual_to = [e.lower() for _, e in getaddresses(msg.get_all("To", []))]
    if (actual_from != MAILBOX.lower() or actual_to != [lead.email.lower()]
            or msg.get("Cc") or msg.get("Bcc")
            or str(msg.get("X-Superfris-Lead-ID") or "") != str(lead.id)
            or not str(msg.get("Subject", "")).strip()):
        raise ValueError("DRAFT_HEADERS_DIFFER_FROM_APPROVAL")
    if not any(p.get_content_type() in ("text/plain", "text/html") for p in msg.walk()):
        raise ValueError("DRAFT_CONTENT_MISSING")
    return msg, uids[0], sent_folder


def _append_optout(msg) -> None:
    """Make a reply-based opt-out available in both MIME body versions."""
    plain_note = "\n\nGeen berichten meer van Superfris? Antwoord met 'afmelden'."
    html_note = '<p style="font-size:12px;color:#555">Geen berichten meer van Superfris? Antwoord met <strong>afmelden</strong>.</p>'
    for part in msg.walk():
        if part.get_content_disposition() == "attachment":
            continue
        typ = part.get_content_type()
        if typ not in ("text/plain", "text/html"):
            continue
        body = part.get_content()
        if "afmelden" in body.lower():
            continue
        if typ == "text/plain":
            part.set_content(body + plain_note, subtype="plain", charset="utf-8")
        else:
            part.set_content(body + html_note, subtype="html", charset="utf-8")


def send_one(db: Session) -> dict:
    if not _enabled() or not _network_allowed() or _paused(db):
        return {"status": "blocked", "reason": status(db)["blocked_reason"]}
    local = datetime.now(timezone.utc).astimezone(settings.local_timezone)
    if not 9 <= local.hour < 19:
        return {"status": "outside_send_hours"}
    if not _guard.acquire(blocking=False):
        return {"status": "busy"}
    try:
        # Serialize reservation and daily quota across old/new service instances.
        if db.bind.dialect.name == "postgresql":
            db.execute(sql_text("SELECT pg_advisory_xact_lock(29102610)"))
        state = status(db)
        if state["attempts_today"] >= MAX_SENDS_PER_DAY:
            db.rollback()
            return {"status": "daily_limit"}
        last = db.scalar(select(func.max(SuperfrisSendAuthorization.attempted_at)))
        if last and (datetime.utcnow() - last).total_seconds() < SEND_INTERVAL_SECONDS:
            db.rollback()
            return {"status": "spacing"}
        queued = db.scalars(select(SuperfrisSendAuthorization).where(
            SuperfrisSendAuthorization.status == "approved"
        ).order_by(SuperfrisSendAuthorization.approved_at).limit(1)).first()
        if not queued:
            db.rollback()
            return {"status": "no_approved_drafts"}
        lead_id = queued.lead_id
        lead = db.get(SuperfrisLead, lead_id)
        if not lead or lead.status != "drafted" or not queued.basis in BASIS or len(queued.evidence) < 16:
            queued.status = "blocked"
            queued.error_type = "approval_invalid"
            db.commit()
            return {"status": "approval_invalid"}
        queued.status = "sending"
        queued.attempted_at = datetime.utcnow()
        db.commit()  # Irreversible reservation before network operations.
        client = None
        try:
            pwd = os.getenv("SUPERFRIS_STRATO_PASSWORD", "")
            if not pwd:
                raise RuntimeError("STRATO_PASSWORD_MISSING")
            client = imaplib.IMAP4_SSL("imap.strato.de", 993, timeout=25)
            client.login(MAILBOX, pwd)
            msg, uid, sent_folder = _read_approved_strato_draft(client, lead)
            _append_optout(msg)
            with smtplib.SMTP_SSL("smtp.strato.de", 465, timeout=25,
                                  context=ssl.create_default_context()) as smtp:
                smtp.login(MAILBOX, pwd)
                refused = smtp.send_message(msg, from_addr=MAILBOX, to_addrs=[lead.email])
                if refused:
                    raise RuntimeError("SMTP_REJECTED_RECIPIENT")
            # Mark success immediately upon SMTP's acceptance before IMAP cleanup.
            row = db.get(SuperfrisSendAuthorization, lead_id)
            row.status = "sent"
            row.sent_at = datetime.utcnow()
            row.message_id = str(msg.get("Message-ID", ""))[:255]
            lead.status = "sent"
            db.commit()
            try:
                mailbox = '"' + sent_folder + '"'
                code, _ = client.append(mailbox, r"(\\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
                if code != "OK":
                    raise RuntimeError("SENT_FOLDER_APPEND_FAILED")
                client.uid("STORE", uid, "+FLAGS", r"(\\Deleted)")
            except Exception as archive_exc:
                logger.warning("Superfris SMTP accepted, IMAP Sent archival failed: %s", type(archive_exc).__name__)
            logger.warning("Superfris approved mail SMTP accepted lead_id=%s sent=1", lead_id)
            return {"status": "sent", "lead_id": lead_id}
        except Exception as exc:
            db.rollback()
            row = db.get(SuperfrisSendAuthorization, lead_id)
            if row and row.status == "sending":
                # Even a network error can mean SMTP accepted the message;
                # never retry automatically.
                row.status = "uncertain"
                row.error_type = type(exc).__name__[:100]
                db.commit()
            logger.warning("Superfris approved send unconfirmed lead_id=%s error=%s", lead_id, type(exc).__name__)
            return {"status": "uncertain", "lead_id": lead_id, "error_type": type(exc).__name__}
        finally:
            if client:
                try:
                    client.logout()
                except Exception:
                    pass
    finally:
        _guard.release()


def run_sender_loop(stop_event: threading.Event) -> None:
    """Each iteration sends at most one approved mail; 10s is the minimum delay."""
    while not stop_event.is_set():
        if _enabled() and _network_allowed():
            db = SessionLocal()
            try:
                result = send_one(db)
                if result.get("status") in ("sent", "uncertain"):
                    logger.warning("Superfris sender result %s", result)
            except Exception:
                logger.exception("Superfris sender worker error")
            finally:
                db.close()
        stop_event.wait(SEND_INTERVAL_SECONDS)
