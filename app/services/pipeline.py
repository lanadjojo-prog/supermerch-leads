from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Campaign, Lead
from .crawler import crawl_website
from .discovery import discover_google_places, normalize_domain
from .intelligence import analyze_company, generate_outreach
from .scoring import score_lead


def choose_best_email(emails: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    if not emails:
        return None, None
    priority = ("marketing@", "hr@", "people@", "office@", "hello@", "info@", "contact@")
    return sorted(
        emails,
        key=lambda item: next((i for i, p in enumerate(priority) if item[0].startswith(p)), 999),
    )[0]


def run_campaign(db: Session, campaign: Campaign) -> dict:
    campaign.status = "running"
    campaign.last_run_at = datetime.utcnow()
    db.commit()

    try:
        companies = discover_google_places(campaign.search_query, campaign.region, campaign.target_count)
    except Exception:
        campaign.status = "error"
        db.commit()
        raise

    created = analysed = qualified = 0

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
            lead.status = "crawl_failed"
            db.commit()
            continue

        excerpt = crawl.combined_text[:50000]
        lead.crawl_excerpt = excerpt

        try:
            analysis = analyze_company(company.name, campaign.niche, excerpt)
        except Exception as exc:
            lead.status = "analysis_failed"
            lead.lead_reason = f"Analysefout: {type(exc).__name__}"
            db.commit()
            continue

        analysed += 1
        scored = score_lead(analysis)
        lead.score = scored.score
        lead.score_reasons = "\n".join(scored.reasons)

        for field in (
            "industry", "company_summary", "employee_signal", "vacancies_signal",
            "employer_branding_signal", "event_signal", "growth_signal", "merch_signal",
            "recommended_offer", "lead_reason",
        ):
            setattr(lead, field, analysis.get(field))

        lead.email, lead.email_source_url = choose_best_email(crawl.emails)

        if lead.score >= 60:
            lead.status = "ready_for_review"
            qualified += 1
            try:
                lead.outreach_text = generate_outreach(company.name, lead.contact_name, analysis)
            except Exception:
                lead.outreach_text = None
        elif lead.score >= 40:
            lead.status = "possible"
        else:
            lead.status = "rejected"
        db.commit()

    campaign.status = "complete"
    db.commit()
    return {"created": created, "analysed": analysed, "qualified": qualified}
