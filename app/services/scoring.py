from dataclasses import dataclass


@dataclass
class ScoreResult:
    score: int
    reasons: list[str]


def positive(value: str | None) -> bool:
    return (value or "").lower() in {"yes", "true", "high", "strong", "ja", "sterk", "present"}


def score_lead(analysis: dict) -> ScoreResult:
    score = 0
    reasons: list[str] = []
    rules = [
        ("vacancies_signal", 20, "Actieve vacatures / werving"),
        ("employer_branding_signal", 15, "Duidelijke employer branding"),
        ("growth_signal", 15, "Groei- of uitbreidingssignaal"),
        ("event_signal", 10, "Events / beurs / activatie relevant"),
    ]
    for key, points, label in rules:
        if positive(analysis.get(key)):
            score += points
            reasons.append(f"+{points} {label}")

    employee = (analysis.get("employee_signal") or "").lower()
    if employee and employee not in {"unknown", "onbekend", "0", "1"}:
        score += 10
        reasons.append("+10 Team/organisatie zichtbaar")
    if analysis.get("recommended_offer"):
        score += 20
        reasons.append("+20 Concrete merchandise-kans gevonden")
    if positive(analysis.get("merch_signal")):
        score += 5
        reasons.append("+5 Merchandise/bedrijfskleding al zichtbaar")
    if not analysis.get("lead_reason"):
        score -= 15
        reasons.append("-15 Geen concrete aanleiding")
    return ScoreResult(score=max(0, min(score, 100)), reasons=reasons)
