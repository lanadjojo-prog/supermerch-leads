from __future__ import annotations

import logging
import re
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import TenderOpportunity, TenderOutreach

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"(?i)\\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}\\b")
RESTRICTED_PATTERNS = (
    r"uitsluitend\\s+(?:via|door middel van)\\s+tenderned",
    r"communicatie.{0,80}uitsluitend.{0,80}tenderned",
    r"vragen.{0,80}(?:via|in)\\s+tenderned",
    r"vragen.{0,80}uitsluitend",
    r"contact.{0,80}niet\\s+toegestaan",
    r"rechtstreeks.{0,80}contact.{0,80}niet",
)

def _extract_emails(text: str) -> list[str]:
    found = []
    for email in EMAIL_RE.findall(text or ""):
        email = email.strip(".,;:()[]<>").lower()
        if email.endswith("@supermerch.nl"):
            continue
        if any(token in email for token in ("noreply", "no-reply", "donotreply")):
            continue
        if email not in found:
            found.append(email)
    return found

def _restricted(text: str) -> tuple[bool, str]:
    haystack = (text or "").lower()
    for pattern in RESTRICTED_PATTERNS:
        if re.search(pattern, haystack, re.I | re.S):
            return True, "Communicatie of vragen moeten volgens de aanbestedingsinformatie via TenderNed lopen."
    return False, ""

def _request_summary(tender: TenderOpportunity) -> str:
    value = (tender.products or tender.ai_summary or tender.title or "").strip()
    return value[:700]

def _draft_body(tender: TenderOpportunity) -> str:
    need = _request_summary(tender)
    return (
        "Goedendag,\n\n"
        f"Ik kwam jullie aanvraag voor {tender.title} tegen en zag dat jullie op zoek zijn naar {need}.\n\n"
        "Dit sluit goed aan bij wat wij bij SuperMerch doen. Wij kunnen dit verzorgen en denken daarbij ook mee over productkeuze, bedrukking, ontwerp, aantallen, levering en budget.\n\n"
        "Indien gewenst maken we vrijblijvend een eerste voorstel inclusief ontwerp, zodat jullie direct een beeld hebben van de mogelijkheden.\n\n"
        "Mocht het interessant zijn, dan kijk ik graag even mee naar jullie wensen."
    )

def prepare_tender_outreach(db: Session, tender: TenderOpportunity) -> TenderOutreach:
    existing = db.scalar(select(TenderOutreach).where(TenderOutreach.opportunity_id == tender.id))
    if existing and existing.draft_body:
        return existing

    raw = tender.raw_text or tender.description or ""
    emails = _extract_emails(raw)
    restricted, restriction_reason = _restricted(raw)
    contact_email = emails[0] if emails else None

    if restricted:
        route = "tenderned"
        reason = restriction_reason
    elif contact_email:
        route = "email_draft"
        reason = "Openbaar e-mailadres gevonden. Concept voorbereid; niet automatisch verzonden zonder aantoonbare toestemming of geldige uitzondering voor commerciële e-mail."
    else:
        route = "contact_missing"
        reason = "Geen openbaar e-mailadres in de opgehaalde aanbestedingsdata gevonden."

    row = existing or TenderOutreach(opportunity_id=tender.id)
    row.contact_email = contact_email
    row.contact_source = tender.source_url
    row.contact_route = route
    row.policy_reason = reason
    row.request_summary = _request_summary(tender)
    row.subject = "Naar aanleiding van jullie aanvraag"
    row.draft_body = _draft_body(tender)
    row.send_status = "draft_only"
    if existing is None:
        db.add(row)
    db.commit()
    db.refresh(row)
    return row

def prepare_tender_outreach_batch(db: Session, limit: int = 50) -> dict:
    tenders = db.scalars(
        select(TenderOpportunity)
        .where(TenderOpportunity.status != "rejected")
        .order_by(TenderOpportunity.ai_score.desc(), TenderOpportunity.created_at.desc())
        .limit(limit)
    ).all()
    prepared = email_drafts = tenderned = missing = 0
    for tender in tenders:
        try:
            row = prepare_tender_outreach(db, tender)
            prepared += 1
            if row.contact_route == "email_draft":
                email_drafts += 1
            elif row.contact_route == "tenderned":
                tenderned += 1
            else:
                missing += 1
        except Exception:
            db.rollback()
            logger.exception("Tender outreach preparation failed for %s", tender.id)
    result = {"prepared": prepared, "email_drafts": email_drafts, "tenderned": tenderned, "contact_missing": missing, "sent": 0}
    logger.warning("Tender outreach prepared: %s", result)
    return result

def prepare_tender_outreach_batch_background() -> None:
    db = SessionLocal()
    try:
        prepare_tender_outreach_batch(db)
    finally:
        db.close()
