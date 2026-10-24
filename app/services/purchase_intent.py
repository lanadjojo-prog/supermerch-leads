"""Evidence-first purchase opportunity detector for public company websites.

Only explicit dated evidence is promoted. Undated/event-list pages are not
treated as current buyer intent. Existing generic trigger detection remains
available separately for research context, never for automatic qualification.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4,
    "mei": 5, "juni": 6, "juli": 7, "augustus": 8,
    "september": 9, "oktober": 10, "november": 11, "december": 12,
}
DATE_RE = re.compile(
    r"\b(?P<iso>20\d{2}-\d{1,2}-\d{1,2})\b|"
    r"\b(?P<d>\d{1,2})[/-](?P<m>\d{1,2})[/-](?P<y>20\d{2})\b|"
    r"\b(?P<day>\d{1,2})\s+(?P<month>" + "|".join(MONTHS) +
    r")\s+(?P<year>20\d{2})\b", re.I
)
REQUEST_RE = re.compile(
    r"\b(?:wij|we|onze organisatie)\s+(?:zijn\s+)?(?:op zoek naar|zoeken)\s+"
    r"(?:een\s+)?(?:leverancier|producent|drukker|merchandisepartner|"
    r"bedrijf\s+voor|partij\s+voor)\b|"
    r"\b(?:leverancier|producent)\s+gezocht\s+voor\b|"
    r"\b(?:aanbesteding|inkoopaanvraag|offerteaanvraag)\s+"
    r"(?:voor\s+)?(?:merchandise|bedrukte|promotieartikelen|bedrijfskleding|teamkleding)\b",
    re.I,
)
PRODUCT_RE = re.compile(
    r"\b(?:merchandise|merch|hoodies?|t-shirts?|shirts?|sokken|"
    r"bedrijfskleding|teamkleding|promotieartikelen|relatiegeschenken|"
    r"supporterssjaals?|give-aways?|drukwerk|bedrukte\s+kleding)\b", re.I
)
EVENT_RE = re.compile(
    r"\b(?:festival|toernooi|sportevenement|open\s+dag|bedrijfsfeest|"
    r"jubileum|lustrum|beurs|congres|evenement|heropening|"
    r"nieuwe\s+vestiging|opening)\b", re.I
)


def _dated_matches(text: str) -> list[tuple[date, int, int]]:
    result = []
    for match in DATE_RE.finditer(text):
        try:
            if match.group("iso"):
                dt = date.fromisoformat(match.group("iso"))
            elif match.group("d"):
                dt = date(int(match.group("y")), int(match.group("m")), int(match.group("d")))
            else:
                dt = date(int(match.group("year")), MONTHS[match.group("month").lower()],
                          int(match.group("day")))
            result.append((dt, match.start(), match.end()))
        except (ValueError, TypeError):
            continue
    return result


def detect_purchase_opportunities(
    pages: list[tuple[str, str]], *, today: date | None = None
) -> list[dict]:
    """Return source-backed, dated buying requests or upcoming opportunities.

    Event announcements are NOT asserted to be requests to purchase.
    """
    now = today or datetime.now().date()
    found = []
    for url, text in pages:
        if not url.startswith(("http://", "https://")):
            continue
        # Web crawls can contain very long menus. Require proximity between
        # a signal and a full date to avoid using unrelated footer dates.
        dates = _dated_matches(text or "")
        if not dates:
            continue
        for kind, pattern in (("intent_active_request", REQUEST_RE),
                              ("intent_upcoming_event", EVENT_RE)):
            for match in pattern.finditer(text or ""):
                eligible_dates = [
                    (dt, start, end) for dt, start, end in dates
                    if min(abs(start - match.end()), abs(match.start() - end)) <= 115
                ]
                if not eligible_dates:
                    continue
                # Bind to the closest full date, not the newest year on the page.
                dt, dstart, dend = min(
                    eligible_dates, key=lambda item:
                    min(abs(item[1] - match.end()), abs(match.start() - item[2]))
                )
                days = (dt - now).days
                if kind == "intent_active_request":
                    if not (-30 <= days <= 14):
                        continue
                    # A procurement phrase must concern branded products;
                    # ignore requests for unrelated suppliers.
                    excerpt = text[max(0, min(match.start(), dstart)-115):
                                   min(len(text), max(match.end(), dend)+115)]
                    if not PRODUCT_RE.search(excerpt):
                        continue
                    label = "Actuele openbare inkoopvraag (controleren)"
                    offer = "Concreet product- en prijsvoorstel passend bij de aanvraag"
                    strength = 95
                else:
                    if not (0 <= days <= 180):
                        continue
                    label = "Aankomend evenement (mogelijke merchbehoefte)"
                    offer = "Merchandisevoorstel passend bij het aangekondigde evenement"
                    strength = 70 if days <= 90 else 55
                start = max(0, min(match.start(), dstart) - 95)
                end = min(len(text), max(match.end(), dend) + 95)
                evidence = re.sub(r"\s+", " ", text[start:end]).strip()
                found.append({
                    "trigger_type": kind,
                    "label": label,
                    "strength": strength,
                    "evidence": evidence[:400],
                    "source_url": url,
                    "event_date": dt.isoformat(),
                    "days_until": days,
                    "recommended_offer": offer,
                    "reason": f"{label}: {evidence[:210]} (bron: {url})",
                })
    # Prefer direct procurement evidence over a generic event mention.
    found.sort(key=lambda x: (x["strength"], -abs(x["days_until"])), reverse=True)
    unique = []
    seen = set()
    for candidate in found:
        key = (candidate["trigger_type"], candidate["source_url"])
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
        if len(unique) == 4:
            break
    return unique
