from __future__ import annotations

import hashlib
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



def outreach_variant(company_name: str) -> str:
    normalized = company_name.strip().lower().encode("utf-8")
    bucket = hashlib.sha256(normalized).digest()[0] % 2
    return "A" if bucket == 0 else "B"


def outreach_subject(company_name: str, variant: str | None = None) -> str:
    variant = variant or outreach_variant(company_name)
    if variant == "A":
        return f"Een idee voor {company_name}"
    return f"Iets uitwerken voor {company_name}?"


def generate_outreach(company_name: str, contact_name: str | None, analysis: dict) -> str:
    variant = outreach_variant(company_name)
    reason = str(analysis.get("lead_reason") or "").strip()
    summary = str(analysis.get("company_summary") or "").strip()

    if not settings.openai_api_key:
        if variant == "A":
            return (
                "Hi!\n\n"
                f"Ik kwam {company_name} tegen en dacht dat ik je even een kort berichtje zou sturen.\n\n"
                "Bij SuperMerch maken we custom kleding en merchandise voor bedrijven en teams. "
                "Om meteen iets concreets te laten zien, kunnen we binnen 24 uur vrijblijvend een eerste "
                "ontwerpvoorstel in jullie huisstijl maken.\n\n"
                f"Zal ik iets voor {company_name} uitwerken?"
            )
        return (
            "Hi!\n\n"
            "Ik ben Chris van SuperMerch. Bij SuperMerch maken we custom kleding en merchandise voor "
            "bedrijven en teams.\n\n"
            "In plaats van meteen een offerte te sturen, laten we liever eerst iets zien: binnen 24 uur "
            "kunnen we vrijblijvend een eerste ontwerpvoorstel in jullie huisstijl maken.\n\n"
            f"Lijkt het je leuk als ik iets voor {company_name} laat uitwerken?"
        )

    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)

    if variant == "A":
        variant_instruction = f"""
VARIANT A — persoonlijke aanleiding:
- Open na 'Hi!' met één korte, natuurlijke zin over het bedrijf.
- Gebruik alleen een concrete aanleiding als die echt uit de feitelijke aanleiding/samenvatting blijkt.
- Als de aanleiding generiek is (zoals 'past binnen doelgroep'), doe dan GEEN nep-personalisatie en schrijf gewoon dat je het bedrijf tegenkwam.
- Positioneer daarna SuperMerch kort.
- Benoem maximaal twee relevante productvoorbeelden; niet standaard vier categorieën opsommen.
- Kernbelofte: binnen 24 uur vrijblijvend een eerste ontwerpvoorstel in hun huisstijl.
- Eindig exact met: Zal ik iets voor {company_name} uitwerken?
"""
    else:
        variant_instruction = f"""
VARIANT B — direct design-first:
- Open na 'Hi!' kort en direct vanuit Chris van SuperMerch.
- Leg de nadruk op eerst iets laten zien in plaats van meteen verkopen of een offerte sturen.
- Positioneer SuperMerch als maker van custom kleding en merchandise voor bedrijven en teams.
- Benoem hooguit twee productvoorbeelden als dat natuurlijk past.
- Kernbelofte: binnen 24 uur vrijblijvend een eerste ontwerpvoorstel in hun huisstijl.
- Eindig exact met: Lijkt het je leuk als ik iets voor {company_name} laat uitwerken?
"""

    prompt = f"""
Schrijf één korte Nederlandse eerste cold-outreachmail namens Chris van SuperMerch.

Doel:
- SuperMerch breed positioneren voor custom kleding en merchandise.
- Niet focussen op onboarding, vacatures of employer branding als aanbod.
- De mail moet voelen als een persoonlijk 1-op-1 bericht, niet als een bulkcampagne.
- Maak geen claims of aannames die niet uit de aangeleverde feiten volgen.
- De kernbelofte is een vrijblijvend eerste ontwerpvoorstel binnen 24 uur.

Stijl:
- Begin exact met: Hi!
- Schrijf kort, menselijk, direct en zakelijk informeel.
- Gebruik 'Bij SuperMerch maken we...' wanneer je het bedrijf introduceert.
- Geen marketingjargon, overdreven enthousiasme of slijmerige formuleringen.
- Geen links, knoppen, trackingtekst of afmeldtekst in de mailbody.
- Geen eigen handtekening toevoegen; de verzendlaag voegt de Chris | Supermerch-handtekening toe.
- Geef alleen de mailtekst terug, geen onderwerp en geen HTML.

{variant_instruction}

Bedrijf: {company_name}
Contact: {contact_name or 'onbekend'}
Feitelijke aanleiding: {reason}
Samenvatting: {summary}
"""
    response = client.responses.create(model=settings.openai_model, input=prompt, store=False)
    return response.output_text.strip()
