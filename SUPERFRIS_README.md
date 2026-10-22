# Superfris · aparte leadmachine (concepten, niet versturen)

Doel: dagelijks **maximaal 30 nieuwe conceptmails** opslaan in de bestaande
STRATO-mailbox `gio@superfris.online`, zonder SuperMerch/Zoho te veranderen.

## Stand van zaken

Dit is **voorbereide code** in een geïsoleerde branch. Er is **geen live
mailboxverbinding** en er worden nog geen concepten geproduceerd. De
Workspace 2-service moet eerst de bestaande SuperMerch-database en Zoho-koppeling
veilig hebben gemigreerd; de oorspronkelijke service is geschorst.

## Veilig activeren

1. Herstel/migreer eerst de bestaande Render-PostgreSQL-database en controleer
   dat de bestaande SuperMerch-leadmachine en mailintegratie werken.
2. Deploy de code op de bestaande Workspace 2-service. De nieuwe
   `superfris_leads` en `superfris_draft_attempts` tabellen worden via
   `Base.metadata.create_all` aangemaakt; bestaande tabellen blijven intact.
3. Zet in **Render Environment** (nooit in GitHub-bestanden of chat):
   - `SUPERFRIS_ENABLED=true` (standaard `false`)
   - `SUPERFRIS_STRATO_PASSWORD=<veilig opgeslagen mailboxwachtwoord>`
   - `GOOGLE_PLACES_API_KEY` (bestaande sleutel)
   - `DAILY_JOB_TOKEN` (bestaand intern automatisatietoken)
   - optioneel `OPENAI_API_KEY` voor onderscheidende, feitelijke concepten
4. Controleer via de Basic Auth-beveiligde route
   `GET /api/superfris/status` of de configuratie gereed staat.
5. Start één keer handmatig een veilige run via
   `POST /api/internal/superfris-lead-engine` met de header
   `X-Automation-Token: <token>` en controleer in STRATO Webmail de
   map **Drafts**. Bij een onduidelijk IMAP-resultaat wordt een reservering
   vastgehouden om dubbel opslaan te voorkomen.
6. Pas daarna een identiek token als `SUPERFRIS_DAILY_JOB_TOKEN` in
   GitHub Actions Secrets in. De workflow kan eerst handmatig worden gestart.
   GitHub scheduled workflows draaien alleen wanneer deze workflow op de
   standaardbranch staat; een losse feature- of migratiebranch start de
   dagelijkse planning dus **niet vanzelf**.

## Gedrag

- Zoekt op Google Places naar passende lokale bedrijven rond Oosterhout,
  Breda en andere plaatsen in Noord-Brabant.
- Bezoekt de bedrijfswebsite en vereist dat een e-mailadres werkelijk op
  een pagina van datzelfde domein staat. Geen gegenereerde e-mailadressen.
- Houdt een apart Superfris-prospectbestand bij en slaat andere leads over
  die al in de SuperMerch-database staan.
- Schrijft een korte, persoonlijke e-mail zonder onbewezen uitspraken.
  Met AI-sleutel kan openbare websitecontext verantwoord worden verwerkt;
  zonder sleutel volgt een terughoudende basistekst.
- Slaat maximaal 5 concepten per run en maximaal 30 reserveringen per
  Amsterdamse kalenderdag op, met \\Draft in de STRATO-map `Drafts`.
- Probeert onzekere eerdere IMAP-opslagen niet automatisch opnieuw.
- **Verstuurt nooit e-mail**: er is geen SMTP-verzendpad.
- Omdat e-mailadressen en koopsignalen kunnen ontbreken, is 30 per dag
  een bovengrens/doel, geen garantie.

## Privacy en naleving

De huidige versie bereidt uitsluitend concepten voor. Voor Nederlandse
commerciële acquisitiemails is doorgaans voorafgaande toestemming nodig,
ook richting bedrijven. Controleer vóór verzending of de ontvanger
toestemming gaf of een geldige uitzondering geldt; bewaar bewijs en
respecteer afmeldingen. Deze eerste versie heeft **nog geen** compleet
toestemmings-/uitschrijvingsregister. Schakel dus geen bulkverzending in.
