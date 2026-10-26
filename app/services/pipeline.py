from datetime import datetime
import logging

logger = logging.getLogger(__name__)

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Campaign, Lead, LeadTrigger
from .crawler import crawl_website
from .discovery import normalize_domain
from .openai_discovery import discover_openai
from .intelligence import analyze_company, generate_outreach
from .scoring import score_lead
from .triggers import detect_triggers
from .purchase_intent import detect_purchase_opportunities


def choose_best_email(emails: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    if not emails:
        return None, None
    priority = ("marketing@", "hr@", "people@", "office@", "hello@", "info@", "contact@")
    return sorted(
        emails,
        key=lambda item: next((i for i, p in enumerate(priority) if item[0].startswith(p)), 999),
    )[0]


def _persist_triggers(db: Session, lead: Lead, triggers: list[dict]) -> None:
    for trigger in triggers:
        row = LeadTrigger(
            lead_id=lead.id,
            trigger_type=str(trigger.get("trigger_type") or "unknown"),
            label=str(trigger.get("label") or "Trigger"),
            strength=int(trigger.get("strength") or 0),
            evidence=str(trigger.get("evidence") or "")[:4000] or None,
            source_url=str(trigger.get("source_url") or "")[:1000] or None,
            recommended_offer=str(trigger.get("recommended_offer") or "")[:300] or None,
        )
        db.add(row)
    db.commit()


def _offer_for_opportunity(niche: str, opportunity: dict) -> str:
    """Practical product angle, without guessed prices, stock or production times."""
    if niche in {"Sport & fitness", "Studio's & dans", "Sportclubs"}:
        return "Custom sportsokken, gripsokken of sportshirts in eigen clubstijl"
    if niche in {"Sport fanclubs & supporters", "Carnaval & optochten"}:
        return "Gebreide supporterssjaals, hoodies of shirts met eigen ontwerp"
    if niche in {"Events", "Lustrums & jubileumcommissies"}:
        return "Eventshirts, hoodies en merchandise voor deelnemers of vrijwilligers"
    return opportunity.get("recommended_offer") or "Custom merchandise passend bij de organisatie"


def _apply_trigger_context(analysis: dict, triggers: list[dict]) -> dict:
    if not triggers:
        return analysis

    strongest = triggers[0]
    analysis = dict(analysis)
    analysis["trigger_type"] = strongest.get("trigger_type")
    analysis["trigger_label"] = strongest.get("label")
    analysis["trigger_strength"] = strongest.get("strength")
    analysis["trigger_evidence"] = strongest.get("evidence")
    analysis["trigger_source_url"] = strongest.get("source_url")
    analysis["trigger_event_date"] = strongest.get("event_date")

    # Een concrete trigger is beter voor outreach dan een generieke campagnereden.
    if strongest.get("reason"):
        analysis["lead_reason"] = strongest["reason"]
    if strongest.get("recommended_offer"):
        analysis["recommended_offer"] = strongest["recommended_offer"]
    return analysis


def run_campaign(db: Session, campaign: Campaign) -> dict:
    campaign.status = "running"
    campaign.last_run_at = datetime.utcnow()
    db.commit()

    try:
        companies = discover_openai(campaign.search_query, campaign.region, campaign.target_count)
    except Exception:
        campaign.status = "error"
        db.commit()
        raise

    created = analysed = qualified = trigger_hits = 0
    crawl_failed = analysis_failed = official_contacts = dated_opportunities = 0

    for company in companies:
        domain = normalize_domain(company.website)
        if not domain or db.scalar(select(Lead).where(Lead.domain == domain)):
            continue

        lead = Lead(
            campaign_id=campaign.id,
            company_name=company.name,
            domain=domain,
            website=company.website,
            address=company.address,
            source=company.source,
            status="new",
        )
        db.add(lead)
        db.commit()
        db.refresh(lead)
        created += 1

        crawl = crawl_website(company.website)
        if not crawl.pages:
            crawl_failed += 1
            lead.status = "crawl_failed"
            db.commit()
            continue

        excerpt = crawl.combined_text[:50000]
        lead.crawl_excerpt = excerpt

        # Dated opportunities must be kept separate from generic website keywords.
        opportunities = detect_purchase_opportunities(crawl.pages)
        if opportunities:
            dated_opportunities += 1
        triggers = detect_triggers(crawl.pages)
        if opportunities or triggers:
            trigger_hits += 1
            _persist_triggers(db, lead, opportunities + triggers)

        try:
            analysis = analyze_company(company.name, campaign.niche, excerpt)
            analysis = _apply_trigger_context(analysis, opportunities or triggers)
            if opportunities:
                analysis["recommended_offer"] = _offer_for_opportunity(campaign.niche, opportunities[0])
        except Exception as exc:
            analysis_failed += 1
            logger.warning("Prospect analysis failed (category=%s; model=%s)",
                           type(exc).__name__, settings.openai_model)
            lead.status = "analysis_failed"
            lead.lead_reason = f"Analysefout: {type(exc).__name__}"
            db.commit()
            continue

        analysed += 1
        lead.email, lead.email_source_url = choose_best_email(crawl.emails)
        contact_available = bool(lead.email and lead.email_source_url)
        if contact_available:
            official_contacts += 1
        scored = score_lead(
            analysis, campaign.niche, triggers=triggers,
            opportunities=opportunities, contact_available=contact_available,
        )
        lead.score = scored.score
        lead.score_reasons = "\n".join(scored.reasons)

        for field in (
            "industry", "company_summary", "employee_signal", "vacancies_signal",
            "employer_branding_signal", "event_signal", "growth_signal", "merch_signal",
            "recommended_offer", "lead_reason",
        ):
            setattr(lead, field, analysis.get(field))

        if scored.auto_eligible:
            lead.status = "ready_for_review"
            qualified += 1
            try:
                lead.outreach_text = generate_outreach(company.name, lead.contact_name, analysis)
            except Exception:
                lead.outreach_text = None
        elif lead.score >= 10:
            # Keep researchable leads without allowing generic fit into drafts.
            lead.status = "possible"
        else:
            lead.status = "rejected"
        db.commit()

    campaign.status = "complete"
    db.commit()
    logger.warning(
        "Campaign verified pipeline: query=%s region=%s created=%d crawled=%d "
        "analysis_failed=%d crawl_failed=%d official_contacts=%d dated_intent=%d qualified=%d",
        campaign.search_query, campaign.region, created, analysed, analysis_failed,
        crawl_failed, official_contacts, dated_opportunities, qualified
    )
    return {
        "created": created,
        "analysed": analysed,
        "qualified": qualified,
        "trigger_hits": trigger_hits,
        "analysis_failed": analysis_failed,
        "crawl_failed": crawl_failed,
        "official_contacts": official_contacts,
        "dated_opportunities": dated_opportunities,
    }


def recover_failed_auto_analysis(db: Session, day_iso: str, *, limit: int = 12) -> dict:
    """Retry today's previously crawled but AI-failed prospects after model repair.

    No external email is sent here. Each lead/domain keeps its existing identity;
    only its analysis, verified contact and review status can be changed.
    """
    failed = db.scalars(
        select(Lead).join(Campaign).where(
            Campaign.name.like(f"AUTO {day_iso} |%"),
            Lead.status == "analysis_failed",
            Lead.crawl_excerpt.is_not(None),
        ).order_by(Lead.id.asc()).limit(min(max(limit, 0), 30))
    ).all()
    counts = {"retried": 0, "recovered": 0, "qualified": 0,
              "contacts": 0, "dated_intent": 0, "analysis_errors": 0}
    for lead in failed:
        counts["retried"] += 1
        campaign = db.get(Campaign, lead.campaign_id)
        if campaign is None:
            continue
        # Refresh contact pages so newly supported mailto links are picked up.
        # Never guess a contact or preserve unverified address metadata.
        crawled = crawl_website(lead.website)
        if not crawled.pages:
            continue
        evidence = crawled.combined_text[:50000]
        opportunities = detect_purchase_opportunities(crawled.pages)
        triggers = detect_triggers(crawled.pages)
        if opportunities:
            counts["dated_intent"] += 1
        try:
            analysis = analyze_company(lead.company_name, campaign.niche, evidence)
            analysis = _apply_trigger_context(analysis, opportunities or triggers)
            if opportunities:
                analysis["recommended_offer"] = _offer_for_opportunity(
                    campaign.niche, opportunities[0])
        except Exception as exc:
            counts["analysis_errors"] += 1
            logger.warning("Recovered lead AI analysis still failing (category=%s; model=%s)",
                           type(exc).__name__, settings.openai_model)
            continue
        lead.crawl_excerpt = evidence
        lead.email, lead.email_source_url = choose_best_email(crawled.emails)
        contact_available = bool(lead.email and lead.email_source_url)
        if contact_available:
            counts["contacts"] += 1
        known_evidence = {
            (trigger.trigger_type, trigger.source_url)
            for trigger in lead.triggers
        }
        for item in opportunities + triggers:
            key = (str(item.get("trigger_type")), str(item.get("source_url")))
            if key in known_evidence:
                continue
            _persist_triggers(db, lead, [item])
            known_evidence.add(key)
        scored = score_lead(analysis, campaign.niche, triggers=triggers,
                            opportunities=opportunities,
                            contact_available=contact_available)
        lead.score = scored.score
        lead.score_reasons = "\n".join(scored.reasons)
        for field in (
            "industry", "company_summary", "employee_signal", "vacancies_signal",
            "employer_branding_signal", "event_signal", "growth_signal", "merch_signal",
            "recommended_offer", "lead_reason",
        ):
            setattr(lead, field, analysis.get(field))
        if scored.auto_eligible:
            lead.status = "ready_for_review"
            try:
                lead.outreach_text = generate_outreach(
                    lead.company_name, lead.contact_name, analysis)
            except Exception as exc:
                logger.warning("Recovered lead copy failed (category=%s)", type(exc).__name__)
                lead.outreach_text = None
            if lead.outreach_text:
                counts["qualified"] += 1
        else:
            lead.status = "possible" if lead.score >= 10 else "rejected"
        db.commit()
        counts["recovered"] += 1
    logger.warning("Retry AI-failed leads summary: %s", counts)
    return counts
