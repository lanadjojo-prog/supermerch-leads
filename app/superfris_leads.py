"""Independent Superfris prospect research and STRATO IMAP draft pipeline.

Disabled until SUPERFRIS_ENABLED=true. Never sends an email. Preserves the
existing SuperMerch lead tables, Zoho sender, and send quota unchanged.
"""
from __future__ import annotations

import imaplib
import html
import logging
import os
import re
import threading
import time
from datetime import datetime, time as day_time, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .config import settings
from .database import Base
from .models import ChatCommand, Lead
from .services.crawler import crawl_website
from .services.discovery import normalize_domain
from .services.open_data_discovery import discover_businesses
from .services.pipeline import choose_best_email

logger = logging.getLogger(__name__)
_guard = threading.Lock()
MAILBOX = "gio@superfris.online"
MAX_DRAFTS_PER_DAY = 30
MAX_DRAFTS_PER_PASS = 5
MAX_SEARCHES_PER_PASS = 3
CATEGORIES = (
    ("Horeca", "restaurant", "menu- en drukwerkontwerp"),
    ("Horeca", "lunchroom", "menukaarten en promotiemateriaal"),
    ("Retail", "speciaalzaak", "huisstijl en promotiemateriaal"),
    ("Sport", "sportschool", "campagnebeelden en grafische uitingen"),
    ("Sport", "vechtsportschool", "posters en grafische uitingen"),
    ("Zorg", "fysiotherapiepraktijk", "huisstijl en promotiemateriaal"),
    ("Beauty", "kapsalon", "branding en promotiemateriaal"),
    ("Beauty", "schoonheidssalon", "logo en huisstijl"),
    ("Bouw", "installatiebedrijf", "huisstijl en commerciële vormgeving"),
    ("Bouw", "schildersbedrijf", "huisstijl en drukwerk"),
    ("Evenementen", "evenementenorganisatie", "posters, flyers en artwork"),
    ("Verenigingen", "sportvereniging", "posters en verenigingsdrukwerk"),
)
REGIONS = ("Oosterhout", "Breda", "Tilburg", "Etten-Leur", "Dongen",
           "Waalwijk", "Roosendaal", "Bergen op Zoom", "Den Bosch",
           "Eindhoven", "Rijen", "Made")


class SuperfrisLead(Base):
    __tablename__ = "superfris_leads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_name: Mapped[str] = mapped_column(String(220))
    domain: Mapped[str] = mapped_column(String(220), unique=True, index=True)
    website: Mapped[str] = mapped_column(String(500))
    region: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(220))
    email_source_url: Mapped[str] = mapped_column(String(500))
    subject: Mapped[str] = mapped_column(String(300))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    drafted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SuperfrisDraftAttempt(Base):
    __tablename__ = "superfris_draft_attempts"
    __table_args__ = (UniqueConstraint("lead_id", name="uq_superfris_one_draft_per_lead"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("superfris_leads.id"))
    status: Mapped[str] = mapped_column(String(40), default="reserved")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    error_type: Mapped[str | None] = mapped_column(String(100), nullable=True)


def _today_bounds() -> tuple[datetime, datetime, str]:
    local = datetime.now(timezone.utc).astimezone(settings.local_timezone)
    begin = datetime.combine(local.date(), day_time.min, tzinfo=settings.local_timezone)
    end = begin + timedelta(days=1)
    return begin.astimezone(timezone.utc).replace(tzinfo=None), end.astimezone(timezone.utc).replace(tzinfo=None), local.date().isoformat()


def _draft_attempts_today(db: Session) -> int:
    start, end, _ = _today_bounds()
    return int(db.scalar(select(func.count(SuperfrisDraftAttempt.id)).where(
        SuperfrisDraftAttempt.created_at >= start,
        SuperfrisDraftAttempt.created_at < end,
    )) or 0)


def _verified_company_email(domain: str, emails: list[tuple[str, str]]) -> tuple[str, str] | None:
    # Never use an inferred email or one scraped from a third-party domain.
    for email, source in sorted(emails, key=lambda x: (not x[0].startswith(("info@", "contact@", "hello@")), x[0])):
        host = normalize_domain(source)
        if host != domain and not host.endswith("." + domain):
            continue
        if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", email):
            continue
        return email.lower(), source
    return None


# Stable 1:1:1 A/B/C allocation across new prospects; no auto-sending.
VARIANT_CODES = ("A", "B", "C")


def _variant_for_lead(lead_id: int) -> str:
    return VARIANT_CODES[(int(lead_id) - 1) % len(VARIANT_CODES)]


def _compose_message(company: str, region: str, category: str, offer: str,
                     website_excerpt: str, variant: str = "A") -> str:
    """Three honest, short first-contact hypotheses. No invented criticism."""
    if variant == "A":
        return (
            "Hallo,\\n\\n"
            f"Ik kwam {company} tegen en wilde je iets voorleggen.\\n\\n"
            "Ik ben Giovanni van Superfris. Ik help ondernemers met grafisch ontwerp, "
            "van campagnes en drukwerk tot huisstijlen.\\n\\n"
            "Ik kan vrijblijvend een kleine designpreview voor jullie uitwerken. "
            "Zo zie je direct wat een frisse ontwerpaanpak kan opleveren, zonder verplichtingen.\\n\\n"
            "Zal ik een eerste idee laten zien?"
        )
    if variant == "B":
        return (
            "Hallo,\\n\\n"
            f"Hebben jullie bij {company} weleens grafisch werk liggen waar intern "
            "geen tijd of capaciteit voor is?\\n\\n"
            "Ik ben Giovanni van Superfris en ondersteun bedrijven op afroep met "
            "campagnemateriaal, drukwerk, social visuals en huisstijlen. "
            "Zonder abonnement of langdurige verplichtingen.\\n\\n"
            "Is extra ontwerpcapaciteit af en toe interessant voor jullie?"
        )
    if variant == "C":
        return (
            "Hallo,\\n\\n"
            f"Ik bekeek de website van {company} en wilde één idee met je delen.\\n\\n"
            "Ik ben Giovanni, grafisch ontwerper bij Superfris. "
            f"Voor organisaties in {category.lower()} kan ik bijvoorbeeld helpen met "
            f"{offer}. Ik kan kort laten zien hoe zo'n uiting er voor jullie uit zou kunnen zien.\\n\\n"
            "Zou je het interessant vinden als ik een concreet ontwerpidee laat zien?"
        )
    raise ValueError("Unknown Superfris mail variant")


def _variant_subject(company: str, variant: str) -> str:
    if variant == "A":
        return f"Een vrijblijvend ontwerpidee voor {company}"[:300]
    if variant == "B":
        return "Af en toe extra ontwerpcapaciteit nodig?"
    if variant == "C":
        return f"Een idee voor {company}"[:300]
    raise ValueError("Unknown Superfris mail variant")


SUPERMERCH_LOGO_URL = "https://superfris.online/wp-content/uploads/2026/10/superfris-v7-00.png"
SIGNATURE_NAME = "Giovanni Djojowardi"
SIGNATURE_PHONE = "+31 (0)6 14 99 01 12"


def _body_without_old_signature(content: str) -> str:
    """Remove only the exact legacy automatically-generated text sign-off."""
    value = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    return re.sub(r"\n{1,3}Groet,\s*\nGiovanni\s*\nSuperfris\s*$", "", value, flags=re.I).strip()


def _html_body(content: str) -> str:
    """Safe basic email HTML, escaping all user/company/site-derived content."""
    clean = _body_without_old_signature(content)
    paragraphs = re.split(r"\n\s*\n", clean)
    html_paragraphs = []
    for block in paragraphs:
        lines = [html.escape(s.strip()) for s in block.split("\n")]
        if any(lines):
            html_paragraphs.append(
                '<p style="margin:0 0 17px;font-family:Arial,Helvetica,sans-serif;'
                'font-size:15px;line-height:1.65;color:#171717;">'
                + "<br>".join(lines) + "</p>"
            )
    return "\n".join(html_paragraphs)


def _signature_html() -> str:
    """Use a PUBLIC WordPress media URL rather than a private webmail snippet URL."""
    return (
        '<div style="margin-top:26px;padding-top:18px;border-top:1px solid #e7e7e7;'
        'max-width:440px;font-family:Arial,Helvetica,sans-serif;color:#171717;'
        'font-size:13px;line-height:1.6;">'
        '<p style="margin:0 0 14px;color:#171717;">Met vriendelijke groet,</p>'
        '<img src="' + SUPERMERCH_LOGO_URL + '" alt="Superfris" width="188" '
        'style="display:block;width:188px;max-width:100%;height:auto;border:0;'
        'margin:0 0 12px;" />'
        '<div style="font-size:14px;"><strong>Giovanni</strong> Djojowardi</div>'
        '<div style="height:3px;width:48px;background:#bcf34b;margin:10px 0;"></div>'
        '<div><a href="mailto:Gio@superfris.online" '
        'style="color:#171717;text-decoration:none;">Gio@superfris.online</a></div>'
        '<div><a href="tel:+31614990112" '
        'style="color:#171717;text-decoration:none;">+31 (0)6 14 99 01 12</a></div>'
        '<div><a href="https://www.superfris.online" '
        'style="color:#171717;text-decoration:none;">www.superfris.online</a></div>'
        '<div style="border-top:1px solid #e7e7e7;margin-top:13px;"></div>'
        '</div>'
    )


def _format_draft(to_address: str, subject: str, content: str,
                  *, lead_id: int | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = MAILBOX
    msg["To"] = to_address
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain="superfris.online")
    if lead_id is not None:
        msg["X-Superfris-Lead-ID"] = str(int(lead_id))
        msg["X-Superfris-Variant"] = _variant_for_lead(int(lead_id))
    plain = _body_without_old_signature(content)
    plain += (
        "\n\nMet vriendelijke groet,\n\nGiovanni Djojowardi\n"
        "Gio@superfris.online\n+31 (0)6 14 99 01 12\n"
        "www.superfris.online"
    )
    msg.set_content(plain)
    body_html = (
        '<div style="max-width:640px;background:#ffffff;padding:8px 0;'
        'font-family:Arial,Helvetica,sans-serif;">'
        + _html_body(content) + _signature_html() + "</div>"
    )
    msg.add_alternative(body_html, subtype="html")
    return msg


def strato_mail_probe() -> dict:
    """Read-only STRATO login/folder probe. Never fetches or sends messages."""
    password = os.getenv("SUPERFRIS_STRATO_PASSWORD", "")
    if not password:
        return {"status": "not_configured", "drafts_folder_found": False}
    server = None
    try:
        server = imaplib.IMAP4_SSL("imap.strato.de", 993, timeout=25)
        server.login(MAILBOX, password)
        code, folders = server.list()
        if code != "OK":
            return {"status": "folder_listing_failed", "drafts_folder_found": False}
        folder = _identify_draft_folder(folders or [])
        return {"status": "connected", "drafts_folder_found": bool(folder)}
    except (imaplib.IMAP4.error, OSError, TimeoutError):
        return {"status": "connection_failed", "drafts_folder_found": False}
    finally:
        if server is not None:
            try:
                server.logout()
            except Exception:
                pass


def _identify_draft_folder(folders: list[bytes]) -> str | None:
    r"""Find RFC 6154 \Drafts or the common mailbox folder names."""
    for raw_line in folders:
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", errors="replace")
        matches = re.search(r' "([^"]+)"\s*$', line)
        mailbox = matches.group(1) if matches else line.rsplit(" ", 1)[-1].strip('"')
        flags = line.split(")", 1)[0].lower()
        basename = mailbox.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if "\\drafts" in flags or basename in {"drafts", "draft", "concepten", "entwuerfe", "entwürfe"}:
            # RFC3501 quote mailbox names containing whitespace.
            if '"' in mailbox or "\r" in mailbox or "\n" in mailbox:
                continue
            return mailbox
    return None


def _save_strato_draft(to_address: str, subject: str, content: str, *, lead_id: int | None = None) -> None:
    """APPEND to the mailbox's actual Drafts folder; never calls SMTP."""
    password = os.getenv("SUPERFRIS_STRATO_PASSWORD", "")
    if not password:
        raise RuntimeError("SUPERFRIS_STRATO_PASSWORD not configured")
    msg = _format_draft(to_address, subject, content, lead_id=lead_id)
    server = imaplib.IMAP4_SSL("imap.strato.de", 993, timeout=25)
    try:
        server.login(MAILBOX, password)
        code, folders = server.list()
        if code != "OK":
            raise RuntimeError("STRATO IMAP LIST failed")
        folder = _identify_draft_folder(folders or [])
        if not folder:
            raise RuntimeError("STRATO Drafts folder missing")
        mailbox = f'"{folder}"' if " " in folder else folder
        code, _ = server.append(
            mailbox, r"(\Draft)",
            imaplib.Time2Internaldate(time.time()), msg.as_bytes()
        )
        if code != "OK":
            raise RuntimeError("STRATO IMAP APPEND did not confirm draft")
    finally:
        try:
            server.logout()
        except Exception:
            pass


def run_strato_draft_selftest(db: Session) -> dict:
    """Write exactly one draft to the owner's own mailbox; never send or retry."""
    key = "superfris-strato-imap-selftest-20261023-v1"
    existing = db.scalar(select(ChatCommand).where(ChatCommand.command_id == key))
    if existing:
        return {"status": "already_attempted", "previous_status": existing.status, "sent": 0}
    claim = ChatCommand(command_id=key, action="superfris_selftest", status="running")
    db.add(claim)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"status": "already_attempted", "sent": 0}
    try:
        _save_strato_draft(
            MAILBOX,
            "Superfris – test concept in STRATO",
            "Hallo,\n\nDit is een automatisch opgeslagen TESTCONCEPT voor de Superfris-leadmachine. "
            "Dit bericht is NIET verzonden.\n\nGroet,\nSuperfris"
        )
        claim.status = "complete"
        claim.finished_at = datetime.utcnow()
        db.commit()
        return {"status": "draft_saved", "recipient": "self", "sent": 0}
    except Exception as exc:
        db.rollback()
        claim = db.scalar(select(ChatCommand).where(ChatCommand.command_id == key))
        if claim:
            claim.status = "error"
            claim.finished_at = datetime.utcnow()
            claim.result = type(exc).__name__[:100]
            db.commit()
        logger.warning("Superfris self-test draft unconfirmed: %s", type(exc).__name__)
        return {"status": "draft_unconfirmed", "error_type": type(exc).__name__, "sent": 0}



def _make_drafts(db: Session) -> int:
    if not os.getenv("SUPERFRIS_STRATO_PASSWORD", ""):
        return 0
    available = max(0, MAX_DRAFTS_PER_DAY - _draft_attempts_today(db))
    quota = min(available, MAX_DRAFTS_PER_PASS)
    if not quota:
        return 0
    candidates = db.scalars(select(SuperfrisLead).where(
        SuperfrisLead.status.in_(("ready", "ready_ai")),
    ).order_by(SuperfrisLead.created_at, SuperfrisLead.id).limit(quota * 3)).all()
    made = 0
    attempted_this_pass = 0
    for lead in candidates:
        if attempted_this_pass >= quota or _draft_attempts_today(db) >= MAX_DRAFTS_PER_DAY:
            break
        # Reserve BEFORE APPEND. A timeout can still mean a draft was created.
        attempt = SuperfrisDraftAttempt(lead_id=lead.id)
        db.add(attempt)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            continue
        attempted_this_pass += 1
        try:
            # Recompose only un-drafted, verified prospects, including existing
            # pending leads, so the trial starts without contacting anyone twice.
            variant = _variant_for_lead(lead.id)
            if lead.status == "ready_ai":
                # ChatGPT researched and supplied evidence-backed copy. Keep it.
                # An empty or unverifiable draft is never silently generated.
                if not (lead.subject or "").strip() or not (lead.message or "").strip():
                    raise ValueError("AI-researched lead lacks subject or message")
            else:
                lead.subject = _variant_subject(lead.company_name, variant)
                lead.message = _compose_message(
                    lead.company_name, lead.region, lead.category,
                    next((offer for cat, _query, offer in CATEGORIES if cat == lead.category),
                         "grafische vormgeving en promotiemateriaal"),
                    "", variant=variant,
                )
            _save_strato_draft(lead.email, lead.subject, lead.message, lead_id=lead.id)
            logger.warning("Superfris concept saved variant=%s lead_id=%s sent=0", variant, lead.id)
        except Exception as exc:
            db.rollback()
            attempt = db.get(SuperfrisDraftAttempt, attempt.id)
            if attempt is not None:
                attempt.status = "unknown_or_failed"
                attempt.error_type = type(exc).__name__[:100]
                db.commit()
            logger.warning("Superfris draft uncertain for lead %s: %s", lead.id, type(exc).__name__)
            continue
        attempt.status = "saved"
        lead.status = "drafted"
        lead.drafted_at = datetime.utcnow()
        db.commit()
        made += 1
    return made


def _discover(db: Session, day: str, slot: int) -> int:
    created = 0
    idx = (datetime.fromisoformat(day).toordinal() * 3 + slot * 7) % len(CATEGORIES)
    for n in range(MAX_SEARCHES_PER_PASS):
        category, search, offer = CATEGORIES[(idx + n) % len(CATEGORIES)]
        region = REGIONS[(idx + slot + n * 3) % len(REGIONS)]
        companies = discover_businesses(search, region, limit=10, purpose="design", niche=category)
        for company in companies:
            domain = normalize_domain(company.website)
            if not domain or db.scalar(select(SuperfrisLead.id).where(SuperfrisLead.domain == domain)):
                continue
            if db.scalar(select(Lead.id).where(Lead.domain == domain)):
                continue  # Do not contact the same company for both brands.
            crawled = crawl_website(company.website)
            if not crawled.pages:
                continue
            official = _verified_company_email(domain, crawled.emails)
            if not official:
                continue
            email, source = official
            if db.scalar(select(SuperfrisLead.id).where(SuperfrisLead.email == email)):
                continue
            lead = SuperfrisLead(
                company_name=company.name[:220], domain=domain[:220],
                website=company.website[:500], region=region, category=category,
                email=email[:220], email_source_url=source[:500],
                subject=_variant_subject(company.name, "A"),
                message=_compose_message(company.name, region, category, offer, crawled.combined_text),
            )
            db.add(lead)
            try:
                db.commit()
                created += 1
            except IntegrityError:
                db.rollback()
    return created


def run_superfris_pass(db: Session) -> dict:
    """Idempotent hourly research + max 5 draft saves; safe-by-default off."""
    if os.getenv("SUPERFRIS_ENABLED", "false").lower() not in ("1", "true", "yes", "on"):
        return {"status": "disabled", "sent": 0}
    if not os.getenv("SUPERFRIS_STRATO_PASSWORD", ""):
        return {"status": "strato_mail_not_configured", "sent": 0}
    local_now = datetime.now(timezone.utc).astimezone(settings.local_timezone)
    if not (9 <= local_now.hour < 19):
        return {"status": "outside_window", "sent": 0}
    if _draft_attempts_today(db) >= MAX_DRAFTS_PER_DAY:
        return {"status": "daily_cap_reached", "draft_attempts_today": MAX_DRAFTS_PER_DAY, "sent": 0}
    if not _guard.acquire(blocking=False):
        return {"status": "already_running", "sent": 0}
    try:
        if os.getenv("LEADS_DISCOVERY_PROVIDER", "").lower() == "chatgpt":
            saved = _make_drafts(db)
            return {"status": "chatgpt_leads_ingest_mode",
                    "drafts_created": saved,
                    "pending_drafts": int(db.scalar(select(func.count(SuperfrisLead.id)).where(
                        SuperfrisLead.status.in_(("ready", "ready_ai"))
                    )) or 0), "sent": 0}
        day = local_now.date().isoformat()
        # One-time new pipeline namespace: old failed API slots must not block OSM recovery.
        slot_key = f"superfris-hour-{day}-{local_now.hour:02d}-osm-v2"
        existing = db.scalar(select(ChatCommand).where(ChatCommand.command_id == slot_key))
        if existing is not None:
            old_enough = (datetime.utcnow() - timedelta(minutes=30))
            can_retry = (
                (existing.status == "error" and existing.finished_at
                 and existing.finished_at <= old_enough)
                or (existing.status == "running" and existing.created_at <= old_enough)
            )
            if not can_retry:
                return {"status": "slot_already_claimed", "date": day, "sent": 0}
            existing.status = "running"
            existing.finished_at = None
            existing.result = None
            db.commit()
            record = existing
        else:
            record = ChatCommand(command_id=slot_key, action="superfris_discovery", status="running")
            db.add(record)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return {"status": "slot_already_claimed", "sent": 0}
        try:
            discovery_error = None
            try:
                created = _discover(db, day, local_now.hour)
            except Exception as discovery_exc:
                # Keep converting an existing verified prospect backlog into
                # drafts even when the external company-discovery API is down.
                db.rollback()
                created = 0
                discovery_error = (
                    str(discovery_exc) if str(discovery_exc).startswith("openai_discovery_")
                    else type(discovery_exc).__name__
                )
                logger.exception("Superfris company discovery unavailable: %s", discovery_error)
            drafted = _make_drafts(db)
            record = db.scalar(select(ChatCommand).where(ChatCommand.command_id == slot_key))
            record.status = "error" if discovery_error else "complete"
            record.result = discovery_error
            record.finished_at = datetime.utcnow()
            db.commit()
            return {
                "status": "discovery_blocked" if discovery_error else "complete",
                "discovery_error": discovery_error, "date": day,
                "new_prospects": created, "drafts_created": drafted,
                "draft_attempts_today": _draft_attempts_today(db),
                "strato_credentials_configured": bool(os.getenv("SUPERFRIS_STRATO_PASSWORD")),
                "sent": 0,
            }
        except Exception as exc:
            db.rollback()
            record = db.scalar(select(ChatCommand).where(ChatCommand.command_id == slot_key))
            if record is not None:
                record.status = "error"
                record.result = type(exc).__name__[:100]
                record.finished_at = datetime.utcnow()
                db.commit()
            logger.exception("Superfris research failed")
            return {"status": "error", "error_type": type(exc).__name__, "sent": 0}
    finally:
        _guard.release()



def drain_verified_chatgpt_backlog(db: Session) -> dict:
    """Save up to 30 already-researched drafts without hitting any search API.

    Called only for a date-scoped user-requested catch-up. All writes are to
    STRATO Drafts via IMAP; no SMTP or send endpoint is ever invoked. Existing
    durable per-lead reservations prevent duplicate APPEND on restart.
    """
    if os.getenv("SUPERFRIS_ENABLED", "false").lower() not in ("1", "true", "yes", "on"):
        return {"status": "disabled", "sent": 0}
    local = datetime.now(timezone.utc).astimezone(settings.local_timezone)
    if not (9 <= local.hour < 19):
        return {"status": "outside_window", "sent": 0}
    if not _guard.acquire(blocking=False):
        return {"status": "already_running", "sent": 0}
    try:
        saved = 0
        for _ in range(6):
            n = _make_drafts(db)
            saved += n
            if not n:
                break
        logger.warning("ChatGPT-sourced STRATO backlog saved=%s sent=0", saved)
        return {"status": "complete", "saved": saved, "sent": 0}
    finally:
        _guard.release()


def run_superfris_catchup_pass(db: Session, sequence: int) -> dict:
    """One safe extra research/draft pass for a user-requested catch-up.

    Independently reserved sequence keys allow several attempts in one hour,
    while the shared process guard, prospect deduplication, per-lead APPEND
    reservation and 30/day ceiling apply identically to scheduled passes.
    Never sends SMTP mail.
    """
    if os.getenv("SUPERFRIS_ENABLED", "false").lower() not in ("1", "true", "yes", "on"):
        return {"status": "disabled", "drafts_created": 0, "sent": 0}
    if not os.getenv("SUPERFRIS_STRATO_PASSWORD", ""):
        return {"status": "strato_mail_not_configured", "drafts_created": 0, "sent": 0}
    local = datetime.now(timezone.utc).astimezone(settings.local_timezone)
    if not 9 <= local.hour < 19:
        return {"status": "outside_window", "drafts_created": 0, "sent": 0}
    attempts = _draft_attempts_today(db)
    if attempts >= MAX_DRAFTS_PER_DAY:
        return {"status": "daily_cap_reached", "draft_attempts_today": attempts,
                "drafts_created": 0, "sent": 0}
    if not _guard.acquire(blocking=False):
        return {"status": "already_running", "drafts_created": 0, "sent": 0}
    try:
        key = f"superfris-user-catchup-{local.date().isoformat()}-{int(sequence):03d}"
        if db.scalar(select(ChatCommand.id).where(ChatCommand.command_id == key)):
            return {"status": "slot_already_claimed", "drafts_created": 0, "sent": 0}
        record = ChatCommand(command_id=key, action="superfris_catchup", status="running")
        db.add(record)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return {"status": "slot_already_claimed", "drafts_created": 0, "sent": 0}

        # Drain previously verified, not-yet-drafted prospects first.
        drafted = _make_drafts(db)
        created = 0
        error = None
        if _draft_attempts_today(db) < MAX_DRAFTS_PER_DAY:
            try:
                # Distinct reproducible search rotation for each catch-up slot.
                created = _discover(db, local.date().isoformat(),
                                    slot=local.hour + int(sequence) * 7)
            except Exception as exc:
                db.rollback()
                error = str(exc) if str(exc).startswith(("openai_discovery_", "open_data_")) else type(exc).__name__
                logger.warning("Superfris catch-up discovery error: %s", error)
            drafted += _make_drafts(db)

        record = db.scalar(select(ChatCommand).where(ChatCommand.command_id == key))
        if record:
            record.status = "error" if error else "complete"
            record.result = (error or f"created={created}; drafted={drafted}; sent=0")
            record.finished_at = datetime.utcnow()
            db.commit()
        result = {"status": "discovery_blocked" if error else "complete",
                  "new_prospects": created, "drafts_created": drafted,
                  "draft_attempts_today": _draft_attempts_today(db),
                  "discovery_error": error, "sent": 0}
        logger.warning("Superfris catch-up pass %s: %s", sequence, result)
        return result
    finally:
        _guard.release()


def superfris_stats(db: Session) -> dict:
    start, end, _ = _today_bounds()
    saved_today = int(db.scalar(select(func.count(SuperfrisDraftAttempt.id)).where(
        SuperfrisDraftAttempt.status == "saved",
        SuperfrisDraftAttempt.created_at >= start,
        SuperfrisDraftAttempt.created_at < end,
    )) or 0)
    return {
        "saved_today": saved_today,
        "configured": os.getenv("SUPERFRIS_ENABLED", "false").lower() in ("1", "true", "yes", "on"),
        "mail_password_configured": bool(os.getenv("SUPERFRIS_STRATO_PASSWORD")),
        "pending_drafts": int(db.scalar(select(func.count(SuperfrisLead.id)).where(
            SuperfrisLead.status.in_(("ready", "ready_ai"))
        )) or 0),
        "saved_drafts_total": int(db.scalar(select(func.count(SuperfrisDraftAttempt.id)).where(
            SuperfrisDraftAttempt.status == "saved"
        )) or 0),
        "attempts_today": _draft_attempts_today(db),
        "daily_limit": MAX_DRAFTS_PER_DAY,
        "automatic_sending": False,
    }
