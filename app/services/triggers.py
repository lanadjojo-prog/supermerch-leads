from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(frozen=True)
class DetectedTrigger:
    trigger_type: str
    label: str
    strength: int
    evidence: str
    source_url: str
    recommended_offer: str
    reason: str

    def as_dict(self) -> dict:
        return asdict(self)


TRIGGER_RULES = (
    {
        "trigger_type": "merch_workwear",
        "label": "Merchandise / bedrijfskleding zichtbaar",
        "strength": 34,
        "patterns": (
            r"\bbedrijfskleding\b", r"\bwerkkleding\b", r"\bteamkleding\b",
            r"\bclubkleding\b", r"\bmerchandise\b", r"\bmerch shop\b",
        ),
        "offer": "Custom kleding en merchandise in de bestaande huisstijl",
    },
    {
        "trigger_type": "anniversary",
        "label": "Jubileum / lustrum",
        "strength": 32,
        "patterns": (
            r"\bjubileum\b", r"\blustrum\b", r"\bjubileumjaar\b",
            r"\blustrumjaar\b", r"\bjubileumfeest\b", r"\bjubileumcommissie\b",
        ),
        "offer": "Jubileum- of lustrummerch zoals kleding, drinkwaren, tassen en pins",
    },
    {
        "trigger_type": "rebrand",
        "label": "Rebranding / nieuwe huisstijl",
        "strength": 30,
        "patterns": (
            r"\bnieuwe huisstijl\b", r"\brebrand(?:ing)?\b", r"\bnieuw logo\b",
            r"\bvernieuwde identiteit\b", r"\bnieuwe merkidentiteit\b",
        ),
        "offer": "Merchandise in de nieuwe huisstijl, inclusief gratis ontwerpvoorstel",
    },
    {
        "trigger_type": "opening",
        "label": "Opening / nieuwe vestiging",
        "strength": 29,
        "patterns": (
            r"\bnieuwe vestiging\b", r"\bnieuwe locatie\b", r"\bopening van\b",
            r"\bgrand opening\b", r"\bheropening\b", r"\bopent .* vestiging\b",
        ),
        "offer": "Openings- en teammerch zoals kleding, drinkwaren en give-aways",
    },
    {
        "trigger_type": "trade_show",
        "label": "Beurs / congresdeelname",
        "strength": 28,
        "patterns": (
            r"\bop de beurs\b", r"\bstandnummer\b", r"\bstand [a-z0-9-]{1,8}\b",
            r"\bbeursdeelname\b", r"\bexposant\b", r"\bexhibitor\b",
            r"\bvakbeurs\b", r"\bcongres\b",
        ),
        "offer": "Beursmerch: goodiebags, kleding, drinkwaren en compacte give-aways",
    },
    {
        "trigger_type": "event",
        "label": "Event / activatie",
        "strength": 25,
        "patterns": (
            r"\bevenement\b", r"\bevent\b", r"\bfestival\b", r"\btoernooi\b",
            r"\bbedrijfsfeest\b", r"\bopen dag\b",
        ),
        "offer": "Eventmerch zoals shirts, caps, drinkwaren, tassen en give-aways",
    },
    {
        "trigger_type": "sponsorship",
        "label": "Sponsoring / partnership",
        "strength": 22,
        "patterns": (
            r"\bhoofdsponsor\b", r"\bsponsor van\b", r"\bsponsoring\b",
            r"\bpartner van\b", r"\bofficial partner\b",
        ),
        "offer": "Sponsor- en activatiemerch die merk en partnership zichtbaar maakt",
    },
    {
        "trigger_type": "hiring",
        "label": "Groei / onboarding",
        "strength": 13,
        "patterns": (
            r"\bvacatures?\b", r"\bwerken bij\b", r"\bwe groeien\b",
            r"\bteam uitbreiden\b", r"\bnieuwe collega", r"\bjoin our team\b",
        ),
        "offer": "Onboardingpakket of teammerch voor nieuwe medewerkers",
    },
)

STRONG_TRIGGER_TYPES = {
    "merch_workwear", "anniversary", "rebrand", "opening", "trade_show", "event"
}


def _evidence(text: str, match: re.Match, radius: int = 155) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    snippet = re.sub(r"\s+", " ", text[start:end]).strip(" -–|")
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet += "…"
    return snippet[:420]


def _recency_adjustment(evidence: str) -> int:
    """Downgrade clearly stale dated mentions, without inventing freshness."""
    years = [int(x) for x in re.findall(r"\b20\d{2}\b", evidence)]
    if not years:
        return 0
    current_year = datetime.utcnow().year
    newest = max(years)
    if newest <= current_year - 2:
        return -12
    if newest == current_year - 1:
        return -3
    return 0


def detect_triggers(pages: list[tuple[str, str]]) -> list[dict]:
    found: dict[str, DetectedTrigger] = {}

    for url, text in pages:
        haystack = text or ""
        for rule in TRIGGER_RULES:
            best_match = None
            for pattern in rule["patterns"]:
                match = re.search(pattern, haystack, flags=re.I)
                if match and (best_match is None or match.start() < best_match.start()):
                    best_match = match
            if not best_match:
                continue

            evidence = _evidence(haystack, best_match)
            strength = max(1, int(rule["strength"]) + _recency_adjustment(evidence))
            reason = f'{rule["label"]} gevonden op de website: {evidence}'
            candidate = DetectedTrigger(
                trigger_type=rule["trigger_type"],
                label=rule["label"],
                strength=strength,
                evidence=evidence,
                source_url=url,
                recommended_offer=rule["offer"],
                reason=reason,
            )
            previous = found.get(candidate.trigger_type)
            if previous is None or candidate.strength > previous.strength:
                found[candidate.trigger_type] = candidate

    return [
        item.as_dict()
        for item in sorted(
            found.values(),
            key=lambda item: (item.strength, item.trigger_type),
            reverse=True,
        )
    ]
