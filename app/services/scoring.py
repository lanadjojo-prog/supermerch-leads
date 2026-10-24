"""Opportunity-first scoring. Generic site keywords never count as purchase intent."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScoreResult:
    score: int
    reasons: list[str]
    strong_signal_count: int
    auto_eligible: bool


TARGET_SECTORS = {
    "Carnaval & optochten", "Sport fanclubs & supporters",
    "Lustrums & jubileumcommissies", "Sportclubs", "Sport & fitness",
    "Studio's & dans", "Events", "Studentenorganisaties",
    "Verenigingen & communities", "Franchise & ketens",
}


def positive(value: str | None) -> bool:
    return str(value or "").lower() in {"yes", "true", "high", "strong", "ja", "sterk", "present"}


def score_lead(
    analysis: dict,
    niche: str | None = None,
    triggers: list[dict] | None = None,
    *,
    opportunities: list[dict] | None = None,
    contact_available: bool = False,
) -> ScoreResult:
    """Return a conservative score with a hard verified-opportunity gate.

    Old broad website triggers remain informative but cannot unlock a lead.
    A company with no dated buying signal cannot become auto-eligible.
    """
    reasons = []
    sector = niche or str(analysis.get("industry") or "")
    fit = 20 if sector in TARGET_SECTORS else 10
    score = fit
    reasons.append(f"+{fit} Doelgroepfit: {sector or 'overig'} (geen koopsignaal)")

    opportunities = [o for o in (opportunities or [])
                     if o.get("source_url") and o.get("evidence")
                     and o.get("event_date") and o.get("trigger_type") in {
                         "intent_active_request", "intent_upcoming_event"}]
    best = opportunities[0] if opportunities else None
    if best:
        days = int(best.get("days_until") or 0)
        request = best["trigger_type"] == "intent_active_request"
        intent = 50 if request else (35 if days <= 60 else 25)
        timing = 20 if request or days <= 45 else (12 if days <= 90 else 6)
        score += intent + timing + 10
        reasons.extend([
            f"+{intent} {'Openbare koopvraag' if request else 'Aankomend evenement (kans, geen bewezen vraag)'}",
            f"+{timing} Datering: {best['event_date']} ({days:+d} dagen)",
            f"+10 Controleerbare bron: {best['source_url'][:180]}",
        ])
    else:
        reasons.append("+0 Geen actuele, gedateerde koopaanleiding met bron")
        if triggers:
            reasons.append("+0 Algemene websitetermen geven geen koopintentiepunten")

    if contact_available:
        score += 5
        reasons.append("+5 Zakelijk contactadres gevonden; toestemming NIET vastgesteld")
    else:
        reasons.append("+0 Geen geverifieerd zakelijk contactadres")
    if positive(analysis.get("one_person_signal")):
        score = max(0, score - 15)
        reasons.append("-15 Vermoedelijke eenmanszaak (handmatig beoordelen)")
    score = max(0, min(100, score))

    # Ready for human review is not permission to send unsolicited messages.
    eligible = bool(
        best and score >= 75 and contact_available
        and not positive(analysis.get("one_person_signal"))
        and (best["trigger_type"] == "intent_active_request"
             or 0 <= int(best["days_until"]) <= 90)
    )
    if not eligible:
        reasons.append("REVIEW-BLOK: geen sterk actueel signaal, contact of voldoende score")
    reasons.append("VERZEND-BLOK: toestemming of andere geldige contactgrond eerst controleren")
    return ScoreResult(
        score=score,
        reasons=reasons,
        strong_signal_count=len(opportunities),
        auto_eligible=eligible,
    )
