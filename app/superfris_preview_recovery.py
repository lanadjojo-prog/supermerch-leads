"""Owner-requested preview copy and bounded November 16 mailbox recovery."""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)
VERSION = "20261116"
OFFER = ("Hebben jullie een designopdracht in gedachten? Ik maak daar graag vrijblijvend "
         "een gratis preview voor, zodat jullie vooraf kunnen zien wat ik voor jullie kan betekenen.")
CTA = "Laat gerust weten wat jullie eventueel nodig hebben; ik denk graag met jullie mee."
OPTOUT = "Geen berichten meer van SuperFris? Antwoord met 'afmelden'."


def preview_offer_content(content: str) -> str:
    text = content.replace("\\n", "\n").replace("\r\n", "\n").strip()
    if OFFER in text:
        return text
    text = re.split(r"\n\s*(?:Met vriendelijke groet|Groet|Vriendelijke groet)[,\s]*\n", text, maxsplit=1, flags=re.I)[0]
    kept = []
    for block in re.split(r"\n\s*\n", text):
        if re.search(r"(?:Geen (?:interesse|berichten)|afmelden)", block, re.I):
            continue
        sentences = re.split(r"(?<=[.!?])\s+", block.strip())
        keep = [s for s in sentences if not re.search(
            r"(?:vrijblijvend|preview|zonder verplichtingen|zonder abonnement|"
            r"^Zal ik|^Zou (?:je|u|dat)|^Zouden jullie|interessant voor jullie|"
            r"^Is extra ontwerpcapaciteit|^Ik kan kort laten zien)", s, re.I)]
        if keep:
            kept.append(" ".join(keep))
    if not any("Giovanni" in b for b in kept):
        kept.append("Ik ben Giovanni van SuperFris en help bedrijven met grafisch ontwerp, "
                    "van logo's en huisstijlen tot drukwerk en socialmediavisuals.")
    return "\n\n".join(kept + [OFFER, CTA, OPTOUT])


def recover_pending_previews() -> dict:
    """Back up and rewrite pending owned lead drafts; never send or purge mail."""
    import imaplib
    import os
    import time
    from email import policy
    from email.parser import BytesParser
    from email.utils import getaddresses
    from datetime import datetime
    from sqlalchemy import select, text as sql_text
    from .database import SessionLocal
    from .models import ChatCommand
    from .superfris_leads import MAILBOX, SuperfrisLead, _identify_draft_folder, _format_draft
    from .superfris_sender import SuperfrisSendAuthorization, _paused

    key = "superfris-preview-recovery-20261116-v2"
    db = SessionLocal()
    client = None
    try:
        # Only the explicitly requested existing batch, and only while paused.
        if not _paused(db):
            return {"status": "not_paused", "changed": 0}
        if db.bind.dialect.name == "postgresql":
            db.execute(sql_text("SELECT pg_advisory_xact_lock(16112620)"))
        record = db.scalar(select(ChatCommand).where(ChatCommand.command_id == key))
        if record and record.status == "complete":
            return {"status": "already_complete", "changed": 0}
        if not record:
            record = ChatCommand(command_id=key, action="superfris_preview_recovery", status="running")
            db.add(record)
        db.commit()
        leads = db.scalars(select(SuperfrisLead).join(SuperfrisSendAuthorization).where(
            SuperfrisLead.id <= 176, SuperfrisLead.status == "drafted",
            SuperfrisSendAuthorization.status == "approved"
        ).order_by(SuperfrisLead.id)).all()
        client = imaplib.IMAP4_SSL("imap.strato.de", 993, timeout=30)
        client.login(MAILBOX, os.environ["SUPERFRIS_STRATO_PASSWORD"])
        code, folders = client.list()
        if code != "OK":
            raise RuntimeError("IMAP_LIST_FAILED")
        folder = _identify_draft_folder(folders or [])
        if not folder or client.select('"' + folder + '"')[0] != "OK":
            raise RuntimeError("IMAP_DRAFTS_UNAVAILABLE")
        backup = "Superfris_Preview_Backup_20261116"
        if not any(backup.encode() in row for row in folders or []):
            if client.create(backup)[0] != "OK":
                raise RuntimeError("IMAP_BACKUP_UNAVAILABLE")
        changed = 0
        for lead in leads:
            code, found = client.uid("SEARCH", None, "UNDELETED", "HEADER", "X-Superfris-Lead-ID", str(lead.id))
            if code != "OK":
                raise RuntimeError("IMAP_SEARCH_FAILED")
            exact = []
            for uid in (found[0] or b"").split():
                code, rows = client.uid("FETCH", uid, "(BODY.PEEK[])")
                if code != "OK":
                    raise RuntimeError("IMAP_FETCH_FAILED")
                raw = next((p[1] for p in rows or [] if isinstance(p, tuple) and isinstance(p[1], bytes)), None)
                if raw is None:
                    continue
                msg = BytesParser(policy=policy.default).parsebytes(raw)
                if str(msg.get("X-Superfris-Lead-ID", "")) == str(lead.id):
                    exact.append((uid, msg))
            # Some legacy copies reuse a lead header. Match the actual
            # recipient and subject too; never rewrite the mismatching copy.
            exact = [(uid, msg) for uid, msg in exact if
                     [e.lower() for _, e in getaddresses(msg.get_all("To", []))] == [lead.email.lower()]
                     and str(msg.get("Subject", "")).strip() == lead.subject.strip()
                     and not msg.get("Cc") and not msg.get("Bcc")]
            if not exact:
                # Older manually saved copies do not always carry our lead ID.
                code, candidates = client.uid("SEARCH", None, "UNDELETED", "TO", '"' + lead.email + '"')
                if code != "OK":
                    raise RuntimeError("IMAP_RECIPIENT_SEARCH_FAILED")
                for uid in (candidates[0] or b"").split():
                    code, rows = client.uid("FETCH", uid, "(BODY.PEEK[])")
                    if code != "OK":
                        raise RuntimeError("IMAP_FETCH_FAILED")
                    raw = next((p[1] for p in rows or [] if isinstance(p, tuple) and isinstance(p[1], bytes)), None)
                    if raw is None:
                        continue
                    msg = BytesParser(policy=policy.default).parsebytes(raw)
                    if (str(msg.get("Subject", "")).strip() == lead.subject.strip()
                        and [e.lower() for _, e in getaddresses(msg.get_all("From", []))] == [MAILBOX.lower()]
                        and str(msg.get("X-Superfris-Lead-ID", "")) in ("", str(lead.id))):
                        exact.append((uid, msg))
            revised = preview_offer_content(lead.message)
            ready = [(uid, msg) for uid, msg in exact if msg.get("X-Superfris-Preview-Version") == VERSION]
            if len(ready) > 1 or not exact:
                raise RuntimeError("IMAP_PREVIEW_DRAFT_NOT_UNIQUE_OR_MISSING")
            invalid = [msg for _, msg in exact if
                       [e.lower() for _, e in getaddresses(msg.get_all("To", []))] != [lead.email.lower()]
                       or msg.get("Cc") or msg.get("Bcc")]
            if invalid:
                logger.warning("Superfris preview header review required lead_id=%s to_count=%s cc=%s bcc=%s",
                               lead.id, len(getaddresses(invalid[0].get_all("To", []))),
                               bool(invalid[0].get("Cc")), bool(invalid[0].get("Bcc")))
                authorization = db.get(SuperfrisSendAuthorization, lead.id)
                authorization.status = "blocked"
                authorization.error_type = "draft_headers_require_review"
                db.commit()
                continue
            for uid, msg in exact:
                if msg.get("X-Superfris-Preview-Version") != VERSION:
                    if client.uid("COPY", uid, backup)[0] != "OK":
                        raise RuntimeError("IMAP_BACKUP_COPY_FAILED")
            if not ready:
                fresh = _format_draft(lead.email, lead.subject, revised, lead_id=lead.id)
                fresh["X-Superfris-Preview-Version"] = VERSION
                if client.append('"' + folder + '"', r"(\Draft)", imaplib.Time2Internaldate(time.time()), fresh.as_bytes())[0] != "OK":
                    raise RuntimeError("IMAP_PREVIEW_APPEND_FAILED")
            for uid, msg in exact:
                if msg.get("X-Superfris-Preview-Version") != VERSION:
                    # Recoverable original is in the backup folder. No EXPUNGE.
                    if client.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)")[0] != "OK":
                        raise RuntimeError("IMAP_HIDE_OLD_DRAFT_FAILED")
            lead.message = revised
            db.commit()
            changed += 1
            if changed % 10 == 0:
                logger.warning("Superfris preview recovery progress changed=%s", changed)
        record.status = "complete"
        record.finished_at = datetime.utcnow()
        record.result = "changed=" + str(changed) + "; emails_sent=0; originals_backed_up=1"
        db.commit()
        logger.warning("Superfris preview recovery complete changed=%s", changed)
        return {"status": "complete", "changed": changed}
    except Exception as exc:
        db.rollback()
        detail = str(exc) if re.fullmatch(r"[A-Z_]+", str(exc)) else type(exc).__name__
        logger.warning("Superfris preview recovery stopped error=%s", detail)
        return {"status": "failed", "error_type": type(exc).__name__}
    finally:
        if client:
            try:
                client.logout()
            except Exception:
                pass
        db.close()
