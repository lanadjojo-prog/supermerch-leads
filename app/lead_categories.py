from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class LeadCategory:
    name: str
    queries: tuple[str, ...]


LEAD_CATEGORIES: tuple[LeadCategory, ...] = (
    LeadCategory("Bouw & techniek", ("installatiebedrijf", "loodgieter", "elektricien", "aannemer", "schildersbedrijf", "dakdekker", "timmerbedrijf", "warmtepomp installateur", "kozijnenbedrijf")),
    LeadCategory("Recruitment & detachering", ("recruitmentbureau", "detacheringsbureau", "uitzendbureau", "werving en selectie bureau")),
    LeadCategory("Zakelijke dienstverlening", ("consultancybedrijf", "accountantskantoor", "administratiekantoor", "verzekeringsadviseur", "hypotheekadviseur", "advocatenkantoor", "notariskantoor")),
    LeadCategory("Marketing & creatief", ("marketingbureau", "communicatiebureau", "PR bureau", "designstudio", "reclamebureau", "videoproductiebedrijf", "fotografiebureau")),
    LeadCategory("Technologie", ("softwarebedrijf", "SaaS bedrijf", "IT dienstverlener", "cybersecurity bedrijf", "webbureau", "AI bedrijf", "managed service provider")),
    LeadCategory("Automotive & mobiliteit", ("autobedrijf", "autodealer", "garage", "schadeherstelbedrijf", "leasebedrijf", "bandenbedrijf", "autodetailing bedrijf", "rijschool")),
    LeadCategory("Transport & logistiek", ("transportbedrijf", "logistiek bedrijf", "koeriersdienst", "fulfilment bedrijf", "distributiecentrum", "magazijnbedrijf")),
    LeadCategory("Horeca", ("restaurant", "cafe", "bar", "lunchroom", "koffiezaak", "cocktailbar", "beachclub")),
    LeadCategory("Hospitality & verblijf", ("hotel", "vakantiepark", "camping", "familieresort")),
    LeadCategory("Foodservice", ("cateringbedrijf", "foodtruck", "bakkerij", "slagerij", "ijssalon")),
    LeadCategory("Sport & fitness", ("sportschool", "CrossFit box", "HYROX gym", "personal trainer studio", "bootcamp club")),
    LeadCategory("Sportclubs", ("voetbalclub", "hockeyclub", "tennisclub", "padelclub", "volleybalclub", "basketbalclub", "hardloopclub", "wielerclub", "golfclub")),
    LeadCategory("Studio's & dans", ("yogastudio", "pilatesstudio", "dansschool", "vechtsportschool", "zwemschool")),
    LeadCategory("Onderwijs", ("basisschool", "middelbare school", "MBO school", "hogeschool", "universiteit")),
    LeadCategory("Studentenorganisaties", ("studentenvereniging", "studievereniging", "alumnivereniging")),
    LeadCategory("Zorg", ("fysiotherapiepraktijk", "tandartspraktijk", "orthodontist", "huisartsenpraktijk", "kliniek", "thuiszorgorganisatie", "zorginstelling")),
    LeadCategory("Beauty & wellness", ("kapsalon", "barbershop", "schoonheidssalon", "nagelstudio", "tattoo studio", "beauty clinic", "wellnesscentrum", "spa")),
    LeadCategory("Dierenzorg", ("dierenarts", "dierenkliniek")),
    LeadCategory("Industrie & productie", ("productiebedrijf", "metaalbedrijf", "machinebouwer", "verpakkingsbedrijf", "technische leverancier", "voedselproducent")),
    LeadCategory("Groothandel", ("technische groothandel", "horeca groothandel", "bouwmaterialen groothandel", "voedingsgroothandel", "automotive groothandel", "industriële groothandel")),
    LeadCategory("Retail", ("kledingwinkel", "woonwinkel", "tuincentrum", "speciaalzaak", "elektronicawinkel", "bloemist", "juwelier", "opticien")),
    LeadCategory("E-commerce & D2C", ("webshop", "D2C merk", "kledingmerk", "cosmeticamerk", "lifestyle merk", "subscription box")),
    LeadCategory("Vastgoed", ("makelaar", "vastgoedbeheerder", "projectontwikkelaar", "woningcorporatie", "interieurbedrijf", "verhuisbedrijf")),
    LeadCategory("Events", ("evenementenbureau", "festivalorganisatie", "congresorganisator", "beursorganisator", "sportevenement organisator", "bedrijfsfeest organisator", "wedding planner")),
    LeadCategory("Entertainment", ("theater", "poppodium", "concertzaal", "nachtclub", "bowlingcentrum", "escape room")),
    LeadCategory("Muziek & creators", ("muzieklabel", "podcast studio", "creator agency", "influencer agency", "artiestenbureau", "comedy club")),
    LeadCategory("Verenigingen & communities", ("scoutinggroep", "buurtvereniging", "ondernemersvereniging", "businessclub", "muziekvereniging", "goede doelen organisatie")),
    LeadCategory("Franchise & ketens", ("franchiseorganisatie", "fitnessketen", "horecaketen", "makelaarsketen", "autoservice keten")),
    LeadCategory("Facilitair", ("schoonmaakbedrijf", "beveiligingsbedrijf", "glazenwasser bedrijf", "ongediertebestrijding", "afvalbedrijf", "bedrijfscatering")),
    LeadCategory("Groen & buitenwerk", ("hovenier", "boomverzorger", "tuinarchitect", "bestratingsbedrijf", "loonwerker", "agrarisch bedrijf")),
    LeadCategory("Kinderopvang & familie", ("kinderdagverblijf", "BSO", "kinderopvangorganisatie", "speelparadijs", "kinderfeest bedrijf")),
    LeadCategory("Toerisme & recreatie", ("reisorganisatie", "touroperator", "bootverhuur", "watersportbedrijf", "surfclub", "attractiepark", "museum", "toeristische attractie")),
    LeadCategory("Financieel & fintech", ("fintech bedrijf", "boekhoudsoftware bedrijf", "financieel adviseur", "vermogensbeheerder", "payrollbedrijf")),
    LeadCategory("Startups & scale-ups", ("startup", "scale-up", "tech startup")),
    LeadCategory("Medisch commercieel", ("medische leverancier", "laboratorium", "medtech bedrijf", "medisch opleidingsbedrijf", "farmaceutische dienstverlener")),
    LeadCategory("Food & beverage merken", ("koffiebranderij", "frisdrankmerk", "snackmerk", "food startup", "food merk")),
    LeadCategory("Media", ("mediabureau", "productiehuis", "uitgeverij", "radiostation", "lokale media")),
    LeadCategory("Overheid & semi-overheid", ("gemeente", "bibliotheek", "culturele instelling", "veiligheidsregio")),
    LeadCategory("Recreatie & seizoensorganisaties", ("zomerkamp organisatie", "kerstmarkt organisatie", "carnavalsvereniging", "wintersport organisatie")),
)


AUTO_REGIONS: tuple[str, ...] = (
    "Amsterdam", "Rotterdam", "Den Haag", "Utrecht", "Eindhoven", "Tilburg",
    "Breda", "Den Bosch", "Nijmegen", "Arnhem", "Apeldoorn", "Enschede",
    "Zwolle", "Groningen", "Leeuwarden", "Maastricht", "Haarlem", "Alkmaar",
    "Leiden", "Amersfoort", "Almere", "Dordrecht", "Roosendaal", "Nederland",
)


def iter_daily_searches(day: date):
    """Yield a deterministic, rotating nationwide search sequence for one day."""
    seed = day.toordinal()
    categories = LEAD_CATEGORIES
    regions = AUTO_REGIONS
    category_offset = seed % len(categories)
    region_offset = (seed * 7) % len(regions)

    for region_step in range(len(regions)):
        region = regions[(region_offset + region_step) % len(regions)]
        for category_step in range(len(categories)):
            category = categories[(category_offset + category_step + region_step) % len(categories)]
            query = category.queries[(seed + region_step + category_step) % len(category.queries)]
            yield category.name, query, region
