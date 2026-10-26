"""One-time safe upgrade for original plain-text Superfris IMAP drafts.

NO SMTP, no send endpoint. Only migrate an unchanged leadmachine-created draft
that matches an existing saved database lead by sender, recipient, subject and
full plain text. Leave manually edited drafts alone. Keep old copies in a
dedicated IMAP backup folder.
"""
from __future__ import annotations

import imaplib
import logging
import os
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import parseaddr
from sqlalchemy import select

from .database import SessionLocal
from .superfris_leads import (
    MAILBOX, SuperfrisLead, SuperfrisDraftAttempt,
    _guard, _format_draft, _identify_draft_folder,
)

logger = logging.getLogger(__name__)
BACKUP_FOLDER = "Superfris_Oude_Concepten"
LIMIT = 300


def _decode(s: str) -> str:
    try:
        return str(make_header(decode_header(s or "")))
    except (UnicodeError, ValueError):
        return str(s or "")


def _content(msg):
    part = msg.get_body(preferencelist=("plain",))
    return (part.get_content() if part else "").replace("\r\n", "\n").strip()


def _capabilities(server):
    return {
        b.decode("ascii", "ignore").upper()
        for b in getattr(server, "capabilities", ())
        if isinstance(b, bytes)
    }


def _backup_ready(server, existing_folders) -> bool:
    for line in existing_folders or []:
        if BACKUP_FOLDER.encode() in line:
            return True
    kind, _ = server.create(BACKUP_FOLDER)
    return kind == "OK"


def migrate_existing_superfris_drafts(*, execute: bool = False) -> dict:
    """Preview first. Replace only if backup and scoped deletion are supported."""
    output = {
        "mode": "execute" if execute else "preview",
        "status": "unknown", "mailbox_connected": False,
        "saved_db_leads": 0, "found_exact_legacy_drafts": 0,
        "already_html": 0, "skipped_unmatched": 0,
        "converted": 0, "failed": 0, "emails_sent": 0,
    }
    password = os.getenv("SUPERFRIS_STRATO_PASSWORD", "")
    if not password:
        output["status"] = "missing_mailbox_secret"
        return output
    if not _guard.acquire(blocking=False):
        output["status"] = "busy"
        return output
    db = SessionLocal()
    server = None
    try:
        # DB is the source of truth: never process unknown, unclaimed drafts.
        leads = db.scalars(
            select(SuperfrisLead).join(
                SuperfrisDraftAttempt, SuperfrisDraftAttempt.lead_id == SuperfrisLead.id
            ).where(SuperfrisDraftAttempt.status == "saved")
        ).all()
        output["saved_db_leads"] = len(leads)
        if not leads:
            output["status"] = "no_saved_lead_drafts"
            return output
        matched_by_key = {}
        for lead in leads:
            key = (lead.email.lower().strip(), lead.subject.strip())
            matched_by_key.setdefault(key, []).append(lead)
        server = imaplib.IMAP4_SSL("imap.strato.de", 993, timeout=30)
        server.login(MAILBOX, password)
        kind, folders = server.list()
        if kind != "OK":
            output["status"] = "draft_folder_list_failed"
            return output
        folder = _identify_draft_folder(folders or [])
        if not folder:
            output["status"] = "draft_folder_missing"
            return output
        flags = _capabilities(server)
        safe_move = "MOVE" in flags or "UIDPLUS" in flags
        output["safe_move_supported"] = safe_move
        output["mailbox_connected"] = True
        if execute and not safe_move:
            output["status"] = "server_cannot_safely_replace_drafts"
            return output

        mailbox_name = f'"{folder}"' if " " in folder else folder
        kind, _ = server.select(mailbox_name, readonly=not execute)
        if kind != "OK":
            output["status"] = "draft_folder_select_failed"
            return output
        kind, search = server.uid("SEARCH", None, "FROM", f'"{MAILBOX}"')
        if kind != "OK":
            output["status"] = "imap_search_failed"
            return output
        uids = (search[0] or b"").split()
        output["searched_uids"] = min(len(uids), LIMIT)
        # Skip older messages if the mailbox contains more than LIMIT user drafts.
        already_html_ids = set()
        eligible = []
        for uid in uids[-LIMIT:]:
            kind, rows = server.uid("FETCH", uid, "(BODY.PEEK[])")
            if kind != "OK":
                continue
            raw = next((part[1] for part in rows if isinstance(part, tuple)
                        and len(part) >= 2 and isinstance(part[1], bytes)), None)
            if raw is None:
                continue
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            if parseaddr(msg.get("From", ""))[1].lower() != MAILBOX:
                continue
            recipient = parseaddr(msg.get("To", ""))[1].lower()
            subject = _decode(msg.get("Subject", "")).strip()
            possible = matched_by_key.get((recipient, subject), [])
            if not possible:
                continue
            lead_id = (msg.get("X-Superfris-Lead-ID") or "").strip()
            if msg.get_body(preferencelist=("html",)) is not None:
                if lead_id.isdigit() and any(str(x.id) == lead_id for x in possible):
                    already_html_ids.add(lead_id)
                    output["already_html"] += 1
                continue
            old_text = _content(msg)
            exact = [lead for lead in possible
                     if lead.message.replace("\r\n", "\n").strip() == old_text]
            if len(exact) != 1:
                output["skipped_unmatched"] += 1
                continue
            eligible.append((uid, exact[0]))

        output["found_exact_legacy_drafts"] = len(eligible)
        if not execute:
            output["status"] = "preview_complete"
            return output
        if not eligible:
            output["status"] = "no_legacy_drafts_to_change"
            return output

        # A full UID EXPUNGE deletes only the selected UID, never unrelated
        # deleted mail. If UIDPLUS is unavailable, require UID MOVE instead.
        if not _backup_ready(server, folders):
            output["status"] = "backup_folder_unavailable"
            return output
        for old_uid, lead in eligible:
            try:
                # Keep the exact old content in STRATO before replacing anything.
                if "MOVE" not in flags:
                    kind, _ = server.uid("COPY", old_uid, BACKUP_FOLDER)
                    if kind != "OK":
                        raise RuntimeError("IMAP_COPY_BACKUP_FAILED")
                if str(lead.id) not in already_html_ids:
                    fresh = _format_draft(lead.email, lead.subject, lead.message, lead_id=lead.id)
                    kind, _ = server.append(mailbox_name, r"(\Draft)",
                                             imaplib.Time2Internaldate(None),
                                             fresh.as_bytes())
                    if kind != "OK":
                        raise RuntimeError("IMAP_APPEND_HTML_FAILED")
                    # Within same execution, do not create a second new version.
                    already_html_ids.add(str(lead.id))
                if "MOVE" in flags:
                    kind, _ = server.uid("MOVE", old_uid, BACKUP_FOLDER)
                else:
                    kind, _ = server.uid("STORE", old_uid, "+FLAGS.SILENT", r"(\Deleted)")
                    if kind == "OK":
                        kind, _ = server.uid("EXPUNGE", old_uid)
                if kind != "OK":
                    raise RuntimeError("IMAP_ORIGINAL_ARCHIVE_FAILED")
                output["converted"] += 1
            except (imaplib.IMAP4.error, RuntimeError, OSError) as exc:
                output["failed"] += 1
                logger.warning("Superfris draft replacement skipped/uncertain: %s",
                               type(exc).__name__)
        output["status"] = "completed" if not output["failed"] else "partially_completed"
        return output
    except (imaplib.IMAP4.error, OSError, ValueError) as exc:
        output["status"] = "imap_error"
        output["error_type"] = type(exc).__name__
        return output
    finally:
        if server is not None:
            try:
                server.logout()
            except Exception:
                pass
        db.close()
        _guard.release()
