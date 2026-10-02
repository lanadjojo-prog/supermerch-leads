from dataclasses import dataclass


@dataclass
class ScoreResult:
    score: int
    reasons: list[str]
    strong_signal_count: int
    auto_eligible: bool


def positive(value: str | None) -> bool:
    return (value or "").lower() in {"yes", "true", "high", "strong", "ja", "sterk", "present"}


SECTOR_WEIGHTS = {
    "Carnaval & optochten": 22,
    "Sport fanclubs & supporters": 22,
    "Lustrums & jubileumcommissies": 22,
    "Onderwijs": 18,
    "Bouw & techniek": 16,
    "Zakelijke dienstverlening": 14,
    "Recruitment & detachering": 14,
    "Zorg": 14,
    "Industrie & productie": 14,
    "Transport & logistiek": 14,
    "Groothandel": 13,
    "Verenigingen & communities": 13,
    "Studentenorganisaties": 13,
    "Sportclubs": 13,
    "Sport & fitness": 12,
    "Franchise & ketens": 12,
    "Events": 12,
    "Hospitality & verblijf": 10,
    "Horeca": 10,
    "Entertainment": 10,
    "Toerisme & recreatie": 10,
    "Recreatie & seizoensorganisaties": 10,
    "Financieel & fintech": 9,
    "Overheid & semi-overheid": 9,
    "Technologie": 8,
    "Marketing & creatief": 8,
    "Vastgoed": 8,
    "Retail": 7,
    "Food & beverage merken": 7,
}


STRONG_TRIGGER_TYPES = {
    "merch_workwear", "anniversary", "rebrand", "opening", "trade_show", "event"
}


def score_lead(
    analysis: dict,
    niche: str | None = None,
    triggers: list[dict] | None = None,
) -> ScoreResult:
    score = 0
    reasons: list[str] = []
    strong_signal_count = 0
    triggers = triggers or []

    sector = niche or str(analysis.get("industry") or "")
    sector_points = SECTOR_WEIGHTS.get(sector, 5)
    score += sector_points
    reasons.append(f"+{sector_points} Branchefit: {sector or 'overig'}")

    strong_rules = [
        ("workwear_signal", 30, "Werkkleding/bedrijfskleding/teamkleding expliciet zichtbaar"),
        ("merch_signal", 25, "Merchandise of branded kleding expliciet zichtbaar"),
        ("recurring_event_signal", 20, "Terugkerende events/toernooien/beurzen"),
        ("community_signal", 18, "Leden/supporters/studenten/community"),
        ("multi_location_signal", 15, "Meerdere vestigingen/locaties"),
        ("sponsorship_signal", 15, "Sponsoring/partnerschap zichtbaar"),
        ("anniversary_rebrand_signal", 15, "Jubileum/rebranding/opening"),
        ("event_signal", 12, "Concrete event/beurs/activatie"),
    ]
    for key, points, label in strong_rules:
        if positive(analysis.get(key)):
            score += points
            strong_signal_count += 1
            reasons.append(f"+{points} {label}")

    support_rules = [
        ("growth_signal", 6, "Groei/uitbreiding"),
        ("employer_branding_signal", 6, "Employer branding/teamcommunicatie"),
        ("vacancies_signal", 5, "Actieve vacatures/werving"),
    ]
    for key, points, label in support_rules:
        if positive(analysis.get(key)):
            score += points
            reasons.append(f"+{points} {label}")

    # Deterministische triggerlaag: bewijs + bron op de site. We geven slechts een
    # beperkte bonus om dubbele telling met de AI-signalen te voorkomen.
    strong_trigger_count = 0
    for index, trigger in enumerate(triggers[:3]):
        trigger_type = str(trigger.get("trigger_type") or "")
        strength = int(trigger.get("strength") or 0)
        label = str(trigger.get("label") or trigger_type or "Trigger")
        if trigger_type in STRONG_TRIGGER_TYPES and strength >= 20:
            strong_trigger_count += 1
            bonus = 10 if index == 0 else 4
        elif strength >= 20:
            bonus = 4 if index == 0 else 2
        else:
            # Vacatures/groei zijn nuttig voor personalisatie maar niet sterk genoeg
            # om een lead zelfstandig automatisch te laten mailen.
            bonus = 2 if index == 0 else 0
        if bonus:
            score += bonus
            reasons.append(f"+{bonus} Trigger met bron: {label}")

    employee = (analysis.get("employee_signal") or "").lower().strip()
    if employee and employee not in {"unknown", "onbekend", "0", "1", "1-1", "solo"}:
        score += 8
        reasons.append("+8 Zichtbaar team/organisatie")

    if analysis.get("recommended_offer"):
        reasons.append("+0 Aanbod mogelijk, maar telt niet als koopsignaal")

    one_person = positive(analysis.get("one_person_signal"))
    if one_person:
        score -= 30
        reasons.append("-30 Duidelijke zzp/eenmanszaak")

    lead_reason = str(analysis.get("lead_reason") or "").strip().lower()
    if not lead_reason or "past binnen" in lead_reason or "geselecteerd" in lead_reason:
        score -= 10
        reasons.append("-10 Geen concrete aanleiding")

    score = max(0, min(score, 100))

    # Automatisch mailen vereist minimaal één sterk merchsignaal OF een sterke,
    # concrete trigger met bron. Hiring/vacatures alleen tellen dus niet.
    has_strong_trigger = strong_trigger_count >= 1
    auto_eligible = (strong_signal_count >= 1 or has_strong_trigger) and not one_person and score >= 60

    if strong_signal_count == 0 and not has_strong_trigger:
        reasons.append("AUTO BLOK: geen sterk merchandise-koopsignaal of sterke trigger")
    elif one_person:
        reasons.append("AUTO BLOK: duidelijke zzp/eenmanszaak")
    elif score < 60:
        reasons.append("AUTO BLOK: totaalscore onder 60")

    return ScoreResult(
        score=score,
        reasons=reasons,
        strong_signal_count=strong_signal_count + strong_trigger_count,
        auto_eligible=auto_eligible,
    )
