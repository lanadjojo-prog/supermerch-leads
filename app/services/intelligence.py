from __future__ import annotations

import json
import re

from ..config import settings

ANALYSIS_SCHEMA_HINT = {
    "industry": "string",
    "company_summary": "max 2 zinnen",
    "employee_signal": "bijv. 10-50 / 50-200 / unknown",
    "vacancies_signal": "yes/no",
    "employer_branding_signal": "yes/no",
    "event_signal": "yes/no",
    "growth_signal": "yes/no",
    "merch_signal": "yes/no",
    "recommended_offer": "concreet SuperMerch-aanbod of lege string",
    "lead_reason": "1 feitelijke reden waarom deze lead relevant is",
}


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def heuristic_analysis(company_name: str, website_text: str, niche: str) -> dict:
    t = website_text.lower()
    vacancies = any(k in t for k in ("vacature", "vacatures", "werken bij", "career", "jobs"))
    employer = any(k in t for k in ("employer branding", "werken bij", "ons team", "collega", "medewerker"))
    events = any(k in t for k in ("event", "beurs", "congres", "festival", "sponsoring"))
    growth = any(k in t for k in ("groei", "uitbreiden", "nieuwe vestiging", "we groeien", "join our team"))
    merch = any(k in t for k in ("merchandise", "bedrijfskleding", "teamkleding", "hoodie", "t-shirt", "polo"))

    offer = "Custom kleding & merchandise in eigen huisstijl"
    if vacancies or employer:
        reason = "De website toont actieve werving of employer-branding-signalen."
    elif events:
        reason = "De website toont event- of activatiesignalen."
    elif growth:
        reason = "De website toont een groei- of uitbreidingssignaal."
    else:
        reason = "Het bedrijf past binnen de geselecteerde doelgroep."

    return {
        "industry": niche,
        "company_summary": f"{company_name} is geselecteerd binnen de campagne {niche}.",
        "employee_signal": "unknown",
        "vacancies_signal": "yes" if vacancies else "no",
        "employer_branding_signal": "yes" if employer else "no",
        "event_signal": "yes" if events else "no",
        "growth_signal": "yes" if growth else "no",
        "merch_signal": "yes" if merch else "no",
        "recommended_offer": offer,
        "lead_reason": reason,
    }


def analyze_company(company_name: str, niche: str, website_text: str) -> dict:
    if not settings.openai_api_key:
        return heuristic_analysis(company_name, website_text, niche)

    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    prompt = f"""
Je analyseert een Nederlands bedrijf voor SuperMerch, leverancier van bedrukte kleding en merchandise.
Gebruik UITSLUITEND de websitecontent hieronder. Verzin geen feiten.

Doelgroep/campagne: {niche}
Bedrijf: {company_name}

Geef uitsluitend geldige JSON terug met exact deze velden:
{json.dumps(ANALYSIS_SCHEMA_HINT, ensure_ascii=False, indent=2)}

Regels:
- yes alleen als er een concreet signaal in de tekst staat.
- recommended_offer moet praktisch zijn.
- lead_reason moet verwijzen naar een concreet zichtbaar signaal.

WEBSITECONTENT:
{website_text[:30000]}
"""
    response = client.responses.create(model=settings.openai_model, input=prompt, store=False)
    return _extract_json(response.output_text)


def generate_outreach(company_name: str, contact_name: str | None, analysis: dict) -> str:
    greeting = contact_name or "daar"
    if not settings.openai_api_key:
        return (
            f"Hoi {greeting},\n\n"
            f"Ik kwam {company_name} tegen en dacht dat ik je even een kort bericht zou sturen. "
            "Met SuperMerch maken we custom kleding en merchandise voor bedrijven: "
            "van T-shirts, hoodies en drinkware tot giveaways en andere branded items.\n\n"
            "We denken flexibel mee en maken binnen 24 uur vrijblijvend een eerste ontwerpvoorstel "
            "in jullie huisstijl.\n\n"
            f"Zal ik eens iets voor {company_name} uitwerken?\n\n"
            "Groet,\nGiovanni\nSuperMerch"
        )

    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    prompt = f"""
Schrijf één korte Nederlandse eerste cold-outreachmail namens Giovanni van SuperMerch.

Doel:
- SuperMerch breed positioneren voor custom kleding en merchandise.
- Niet focussen op onboarding, vacatures of employer branding als aanbod.
- Noem voorbeelden zoals kleding, drinkware, giveaways of andere branded items.
- De kernbelofte is: flexibel meedenken + vrijblijvend binnen 24 uur een gratis eerste ontwerpvoorstel.

Stijl:
- 65-95 woorden.
- Menselijk, direct en zakelijk informeel.
- Geen marketingjargon, geen overdreven enthousiasme en geen slijmerige formuleringen.
- Geen aannames dat het bedrijf merchandise nodig heeft.
- Maximaal één concrete observatie uit de analyse, alleen als die echt nuttig en natuurlijk is.
- Eindig met een simpele vraag of je iets voor het bedrijf mag uitwerken.
- Gebruik nette alinea's en een losse handtekening.
- Geef alleen de mailtekst terug, geen onderwerp en geen HTML.

Bedrijf: {company_name}
Contact: {contact_name or 'onbekend'}
Feitelijke aanleiding: {analysis.get('lead_reason')}
Samenvatting: {analysis.get('company_summary')}
"""
    response = client.responses.create(model=settings.openai_model, input=prompt, store=False)
    return response.output_text.strip()
