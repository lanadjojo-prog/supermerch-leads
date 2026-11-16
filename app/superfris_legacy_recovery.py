"""Import only existing older SuperFris outreach drafts requested by the owner."""
from __future__ import annotations

import imaplib
import logging
import os
import re
import time
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from sqlalchemy import select, func, or_

from .database import SessionLocal
from .models import ChatCommand
from .superfris_leads import MAILBOX, SuperfrisLead, SuperfrisDraftAttempt, _identify_draft_folder, _format_draft
from .superfris_preview_recovery import VERSION, preview_offer_content

logger = logging.getLogger(__name__)


def _fetch(client, uid):
    code, rows = client.uid("FETCH", uid, "(BODY.PEEK[])")
    if code != "OK":
        raise RuntimeError("IMAP_FETCH_FAILED")
    raw = next((p[1] for p in rows or [] if isinstance(p, tuple) and isinstance(p[1], bytes)), None)
    if raw is None:
        raise RuntimeError("IMAP_MESSAGE_MISSING")
    return BytesParser(policy=policy.default).parsebytes(raw)


def recover_legacy_outreach() -> dict:
    """Date- and template-scoped import, backed up before any old copy is hidden."""
    from .superfris_sender import SuperfrisSendAuthorization, _sent_folder
    key = "superfris-existing-legacy-outreach-20261116-v1"
    db = SessionLocal()
    client = None
    try:
        row = db.scalar(select(ChatCommand).where(ChatCommand.command_id == key))
        if row:
            return {"status": row.status}
        row = ChatCommand(command_id=key, action="superfris_legacy_recovery", status="running")
        db.add(row)
        db.commit()
        client = imaplib.IMAP4_SSL("imap.strato.de", 993, timeout=30)
        client.login(MAILBOX, os.environ["SUPERFRIS_STRATO_PASSWORD"])
        code, folders = client.list()
        draft_folder = _identify_draft_folder(folders or [])
        sent_folder = _sent_folder(folders or [])
        if code != "OK" or not draft_folder or not sent_folder:
            raise RuntimeError("IMAP_FOLDERS_UNAVAILABLE")
        backup = "Superfris_Legacy_Backup_20261116"
        if not any(backup.encode() in f for f in folders or []):
            if client.create(backup)[0] != "OK":
                raise RuntimeError("IMAP_BACKUP_UNAVAILABLE")
        if client.select('"' + draft_folder + '"')[0] != "OK":
            raise RuntimeError("IMAP_DRAFTS_UNAVAILABLE")
        code, found = client.uid("SEARCH", None, "UNDELETED", "BEFORE", "28-Oct-2026")
        if code != "OK":
            raise RuntimeError("IMAP_SEARCH_FAILED")
        candidates = (found[0] or b"").split()
        imported = already_sent = skipped = 0
        for old_uid in candidates:
            if imported >= 30:
                raise RuntimeError("LEGACY_BATCH_EXCEEDS_THIRTY")
            if client.select('"' + draft_folder + '"')[0] != "OK":
                raise RuntimeError("IMAP_DRAFTS_UNAVAILABLE")
            msg = _fetch(client, old_uid)
            subject = str(msg.get("Subject", "")).strip()
            recipients = getaddresses(msg.get_all("To", []))
            from_addresses = [e.lower() for _, e in getaddresses(msg.get_all("From", []))]
            if (from_addresses != [MAILBOX.lower()] or len(recipients) != 1
                or msg.get("Cc") or msg.get("Bcc")
                or any(p.get_content_disposition() == "attachment" for p in msg.walk())
                or not (subject == "Af en toe extra ontwerpcapaciteit nodig?"
                        or subject.startswith("Een vrijblijvend ontwerpidee voor ")
                        or subject.startswith("Een idee voor "))):
                skipped += 1
                continue
            recipient = recipients[0][1].strip().lower()
            if recipient == MAILBOX.lower() or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", recipient):
                skipped += 1
                continue
            domain = recipient.rsplit("@", 1)[1]
            if db.scalar(select(SuperfrisLead.id).where(or_(
                func.lower(SuperfrisLead.email) == recipient,
                func.lower(SuperfrisLead.domain) == domain
            )).limit(1)):
                skipped += 1
                continue
            part = msg.get_body(preferencelist=("plain",))
            original = part.get_content() if part else ""
            if not original.strip() or "giovanni" not in original.lower() or "superfris" not in original.lower():
                skipped += 1
                continue
            # Check the actual STRATO Sent folder, including manual sends.
            if client.select('"' + sent_folder + '"', readonly=True)[0] != "OK":
                raise RuntimeError("IMAP_SENT_UNAVAILABLE")
            code, matches = client.uid("SEARCH", None, "TO", '"' + recipient + '"')
            if code != "OK":
                raise RuntimeError("IMAP_SENT_SEARCH_FAILED")
            duplicate = False
            for uid in (matches[0] or b"").split():
                sent = _fetch(client, uid)
                if (recipient in [e.lower() for _, e in getaddresses(sent.get_all("To", []))]
                    and str(sent.get("Subject", "")).strip() == subject):
                    duplicate = True
                    break
            if duplicate:
                already_sent += 1
                continue
            if client.select('"' + draft_folder + '"')[0] != "OK":
                raise RuntimeError("IMAP_DRAFTS_UNAVAILABLE")
            if client.uid("COPY", old_uid, backup)[0] != "OK":
                raise RuntimeError("IMAP_BACKUP_FAILED")
            text = preview_offer_content(original)
            company = recipients[0][0].strip() or recipient
            for prefix in ("Een vrijblijvend ontwerpidee voor ", "Een idee voor "):
                if subject.startswith(prefix):
                    company = subject[len(prefix):]
            lead = SuperfrisLead(company_name=company[:220], domain=domain[:220], website="",
                region="Bestaand STRATO-concept", category="Grafisch ontwerp", email=recipient,
                email_source_url="", subject=subject[:300], message=text, status="legacy_importing")
            db.add(lead)
            db.commit()  # Durable identity before APPEND: fail closed on uncertainty.
            fresh = _format_draft(recipient, subject, text, lead_id=lead.id)
            fresh["X-Superfris-Preview-Version"] = VERSION
            if client.append('"' + draft_folder + '"', r"(\Draft)",
                             imaplib.Time2Internaldate(time.time()), fresh.as_bytes())[0] != "OK":
                raise RuntimeError("IMAP_LEGACY_APPEND_UNCONFIRMED")
            if client.uid("STORE", old_uid, "+FLAGS.SILENT", r"(\Deleted)")[0] != "OK":
                raise RuntimeError("IMAP_OLD_COPY_ARCHIVE_FAILED")
            lead.status = "drafted"
            lead.drafted_at = datetime.utcnow()
            db.add(SuperfrisDraftAttempt(lead_id=lead.id, status="saved"))
            db.add(SuperfrisSendAuthorization(lead_id=lead.id, basis="owner_reviewed_batch",
                evidence="16 november 2026: eigenaar vraagt alle bestaande STRATO-ontwerpconcepten te wijzigen met een gratis preview en te versturen met minimaal tien seconden tussenruimte. Oud concept fysiek gelezen; geen zelfde ontvanger/onderwerp gevonden in STRATO Verzonden. Origineel gekopieerd naar herstelmap.",
                status="approved"))
            db.commit()
            imported += 1
        row.status = "complete"
        row.finished_at = datetime.utcnow()
        row.result = f"imported={imported}; already_sent={already_sent}; skipped={skipped}; originals_backed_up=1"
        db.commit()
        logger.warning("Superfris legacy recovery complete imported=%s already_sent=%s skipped=%s", imported, already_sent, skipped)
        return {"status": "complete", "imported": imported}
    except Exception as exc:
        db.rollback()
        detail = str(exc) if re.fullmatch(r"[A-Z_]+", str(exc)) else type(exc).__name__
        logger.warning("Superfris legacy recovery stopped error=%s", detail)
        return {"status": "failed", "error": detail}
    finally:
        if client:
            try:
                client.logout()
            except Exception:
                pass
        db.close()
