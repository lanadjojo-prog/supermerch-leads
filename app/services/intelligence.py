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
    "workwear_signal": "yes/no - werkkleding, bedrijfskleding, uniform of teamkleding expliciet zichtbaar",
    "multi_location_signal": "yes/no - meerdere vestigingen of locaties expliciet zichtbaar",
    "community_signal": "yes/no - leden, supporters, studenten, vrijwilligers of actieve community expliciet zichtbaar",
    "sponsorship_signal": "yes/no - sponsoring, partnerschap of sponsoractivatie expliciet zichtbaar",
    "recurring_event_signal": "yes/no - terugkerende events, toernooien, beurzen of activaties expliciet zichtbaar",
    "anniversary_rebrand_signal": "yes/no - jubileum, rebranding, nieuwe huisstijl of opening expliciet zichtbaar",
    "one_person_signal": "yes/no - duidelijke eenmanszaak/zzp/freelance onderneming",
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
    merch = any(k in t for k in ("merchandise", "bedrijfskleding", "teamkleding", "hoodie", "t-shirt", "polo", "werkkleding", "uniform", "supporterssjaal", "sjaal", "fanwear", "clubkleding", "carnavalskleding"))
    workwear = any(k in t for k in ("bedrijfskleding", "teamkleding", "werkkleding", "uniform", "bedrijfspolo", "werkjas"))
    multi_location = any(k in t for k in ("vestigingen", "onze locaties", "locaties", "filialen", "vestiging in"))
    community = any(k in t for k in ("leden", "supporters", "supportersvereniging", "supportersclub", "fanclub", "vrijwilligers", "community", "studentenvereniging", "studievereniging", "carnavalsvereniging", "carnavalsstichting"))
    sponsorship = any(k in t for k in ("sponsor", "sponsoring", "partners", "partner van"))
    recurring_event = any(k in t for k in ("jaarlijks", "ieder jaar", "toernooi", "beurs", "festival", "congres", "evenementen", "carnaval", "optocht", "lustrum", "jubileumfeest"))
    anniversary_rebrand = any(k in t for k in ("jubileum", "lustrum", "lustrumjaar", "jubileumcommissie", "jarig", "nieuwe huisstijl", "rebranding", "heropening", "opening"))
    one_person = any(k in t for k in ("zzp", "zzp'er", "eenmanszaak", "freelancer", "freelance"))

    if niche == "Carnaval & optochten":
        offer = "Carnavalsmerch zoals shirts, hoodies, sjaals, pins en accessoires in eigen ontwerp"
    elif niche == "Sport fanclubs & supporters":
        offer = "Supportersmerch zoals sjaals, shirts, hoodies, caps en drinkware in clubstijl"
    elif niche == "Lustrums & jubileumcommissies":
        offer = "Lustrummerch zoals kleding, drinkware, tassen en jubileumitems met een eigen ontwerp"
    else:
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
        "workwear_signal": "yes" if workwear else "no",
        "multi_location_signal": "yes" if multi_location else "no",
        "community_signal": "yes" if community else "no",
        "sponsorship_signal": "yes" if sponsorship else "no",
        "recurring_event_signal": "yes" if recurring_event else "no",
        "anniversary_rebrand_signal": "yes" if anniversary_rebrand else "no",
        "one_person_signal": "yes" if one_person else "no",
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
- yes alleen als er een concreet signaal letterlijk of ondubbelzinnig uit de websitecontent blijkt.
- Maak onderscheid tussen algemene bedrijfsactiviteit en echte koopintentie voor kleding/merchandise.
- Vacatures op zichzelf zijn GEEN sterk merchandise-signaal.
- workwear_signal alleen bij expliciete werkkleding/bedrijfskleding/uniform/teamkleding.
- multi_location_signal alleen bij expliciet meerdere vestigingen/locaties.
- community_signal alleen bij expliciete leden/supporters/studenten/vrijwilligers/community.
- recurring_event_signal alleen bij terugkerende events/toernooien/beurzen/activaties, niet bij één losse nieuwsvermelding.
- one_person_signal alleen als duidelijk blijkt dat het een zzp'er/eenmanszaak/freelancer is.
- recommended_offer moet praktisch zijn, maar telt niet als bewijs van koopintentie.
- lead_reason moet de sterkste concrete aanleiding noemen; als die er niet is, zeg dat eerlijk.

WEBSITECONTENT:
{website_text[:30000]}
"""
    response = client.responses.create(model=settings.openai_model, input=prompt, store=False)
    return _extract_json(response.output_text)



def _has_concrete_personalization(analysis: dict | None) -> bool:
    if not analysis:
        return False

    trigger_evidence = str(analysis.get("trigger_evidence") or "").strip()
    if len(trigger_evidence) >= 20:
        return True

    strong_signal_fields = (
        "merch_signal",
        "workwear_signal",
        "multi_location_signal",
        "community_signal",
        "sponsorship_signal",
        "recurring_event_signal",
        "anniversary_rebrand_signal",
        "event_signal",
    )
    if any(str(analysis.get(field) or "").lower() == "yes" for field in strong_signal_fields):
        return True

    reason = str(analysis.get("lead_reason") or "").strip().lower()
    generic_reasons = (
        "",
        "het bedrijf past binnen de geselecteerde doelgroep.",
        "past binnen de geselecteerde doelgroep",
        "geen concrete aanleiding",
    )
    return reason not in generic_reasons and len(reason) >= 24


def outreach_variant(company_name: str, analysis: dict | None = None) -> str:
    """Use website evidence to choose the outreach route."""
    return "A" if _has_concrete_personalization(analysis) else "B"


def outreach_subject(company_name: str, variant: str | None = None) -> str:
    variant = variant or "B"
    if variant == "A":
        return f"Een idee voor {company_name}"
    return f"Merch voor {company_name}"


def generate_outreach(company_name: str, contact_name: str | None, analysis: dict) -> str:
    variant = outreach_variant(company_name, analysis)
    reason = str(analysis.get("lead_reason") or "").strip()
    summary = str(analysis.get("company_summary") or "").strip()
    offer = str(analysis.get("recommended_offer") or "").strip()
    trigger_label = str(analysis.get("trigger_label") or "").strip()
    trigger_evidence = str(analysis.get("trigger_evidence") or "").strip()
    trigger_source_url = str(analysis.get("trigger_source_url") or "").strip()

    if not settings.openai_api_key:
        if variant == "A":
            return (
                "Hi!\n\n"
                "Ik ben Chris van SuperMerch.\n\n"
                f"Ik kwam {company_name} tegen en zag op jullie website een concrete aanleiding om even contact op te nemen.\n\n"
                "Wij helpen organisaties met het volledig uit handen nemen van merchandise: "
                "van productkeuze en design tot productie en levering. Eén aanspreekpunt, zodat je "
                "niet zelf met verschillende leveranciers en ontwerpen hoeft te schakelen.\n\n"
                f"Als je wilt, kan ik een paar concrete ideeën voor {company_name} uitwerken "
                "en eventueel direct een eerste ontwerpvoorstel maken.\n\n"
                f"Zal ik iets voor {company_name} uitwerken?"
            )
        return (
            "Hi!\n\n"
            "Ik ben Chris van SuperMerch.\n\n"
            "Merch nodig voor jullie team, event, klanten of campagne? Wij regelen het complete traject: "
            "van productkeuze en design tot productie en levering. Eén aanspreekpunt, zonder gedoe met "
            "verschillende leveranciers.\n\n"
            f"Als je wilt, denk ik vrijblijvend mee over wat voor {company_name} interessant kan zijn "
            "en kan ik direct een paar ideeën en een eerste ontwerpvoorstel uitwerken.\n\n"
            f"Zal ik iets voor {company_name} uitwerken?"
        )

    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)

    if variant == "A":
        variant_instruction = f"""
VARIANT A — persoonlijke aanleiding op basis van websitecontent:
- Begin exact met:
  Hi!

  Ik ben Chris van SuperMerch.
- Gebruik daarna maximaal één korte, natuurlijke zin over een CONCREET feit dat op de website is gevonden.
- Gebruik uitsluitend de aangeleverde feiten. Geen aannames en geen nep-personalisatie.
- Positioneer SuperMerch als partner die het hele merch-traject uit handen neemt.
- Kernpropositie: productkeuze, design, productie en levering via één aanspreekpunt.
- Koppel hooguit één of twee concrete merch-ideeën aan de gevonden aanleiding als dat logisch is.
- Een eerste ontwerpvoorstel mag als laagdrempelige vervolgstap genoemd worden, maar is NIET de hoofdpropositie.
- Eindig exact met: Zal ik iets voor {company_name} uitwerken?
"""
    else:
        variant_instruction = f"""
VARIANT B — weinig bruikbare websitecontent:
- Begin exact met:
  Hi!

  Ik ben Chris van SuperMerch.
- Doe GEEN verzonnen personalisatie.
- Open daarna kort vanuit het probleem: merch nodig voor team, event, klanten of campagne.
- Positioneer SuperMerch als partner die alles regelt: productkeuze, design, productie en levering.
- Benoem één aanspreekpunt en minder gedoe als voordeel.
- Een eerste ontwerpvoorstel mag als laagdrempelige vervolgstap genoemd worden, maar is NIET de hoofdpropositie.
- Eindig exact met: Zal ik iets voor {company_name} uitwerken?
"""

    prompt = f"""
Schrijf één korte Nederlandse eerste cold-outreachmail namens Chris van SuperMerch.

Doel:
- Nieuwe hoofdpropositie: SuperMerch is de partner in merch en ontzorgt het complete traject.
- Verkoop primair complete ontzorging, niet 'gratis design'.
- Van productkeuze en design tot productie en levering via één aanspreekpunt.
- De mail moet voelen als een persoonlijk 1-op-1 bericht, niet als een bulkcampagne.
- Maak geen claims of aannames die niet uit de aangeleverde feiten volgen.

Stijl:
- Kort, menselijk, direct en zakelijk informeel.
- Geen marketingjargon, overdreven enthousiasme of slijmerige formuleringen.
- Geen links, knoppen, trackingtekst of afmeldtekst in de mailbody.
- Geen eigen handtekening toevoegen; de verzendlaag voegt de SuperMerch-handtekening toe.
- Geef alleen de mailtekst terug, geen onderwerp en geen HTML.

{variant_instruction}

Bedrijf: {company_name}
Contact: {contact_name or 'onbekend'}
Feitelijke aanleiding: {reason}
Samenvatting: {summary}
Passend aanbod: {offer or 'geen specifiek aanbod vastgesteld'}
Trigger: {trigger_label or 'geen aparte trigger'}
Triggerbewijs: {trigger_evidence or 'geen apart triggerbewijs'}
Bronpagina trigger: {trigger_source_url or 'onbekend'}

Extra regels:
- Als er concreet triggerbewijs is, gebruik dat als natuurlijke aanleiding.
- Noem geen bron-URL in de mail zelf.
- Een vacature/groei-signaal alleen is te zwak voor stellige personalisatie; formuleer dan terughoudend.
"""
    response = client.responses.create(model=settings.openai_model, input=prompt, store=False)
    return response.output_text.strip()
