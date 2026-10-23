# Superfris · aparte leadmachine (concepten, niet versturen)

Doel: dagelijks **maximaal 30 nieuwe conceptmails** opslaan in de bestaande
STRATO-mailbox `gio@superfris.online`, zonder SuperMerch/Zoho te veranderen.

## Stand van zaken

Dit is **voorbereide code** in een geïsoleerde branch. Er is **geen live
mailboxverbinding** en er worden nog geen concepten geproduceerd. Workspace 2 functioneert met de bestaande SuperMerch-database en Zoho-concepten. De oorspronkelijke service in de oude workspace is geschorst. Verifieer voor go-live dat Workspace 2 nog steeds gezond is.

## Veilig activeren

1. Controleer bestaande Workspace 2 op een succesvolle SuperMerch-run (22 oktober: 30 Zoho-concepten volgens logs).
2. Deploy de code op de bestaande Workspace 2-service. De nieuwe
   `superfris_leads` en `superfris_draft_attempts` tabellen worden via
   `Base.metadata.create_all` aangemaakt; bestaande tabellen blijven intact.
3. Zet in **Render Environment** (nooit in GitHub-bestanden of chat):
   - `SUPERFRIS_ENABLED=false` tijdens de eerste STRATO-probe; activeer later met `true`.
   - `SUPERFRIS_STRATO_PASSWORD=<veilig opgeslagen mailboxwachtwoord>`
   - `GOOGLE_PLACES_API_KEY` (bestaande sleutel)
   - `DAILY_JOB_TOKEN` (bestaand intern automatisatietoken)
   - optioneel `OPENAI_API_KEY` voor onderscheidende, feitelijke concepten
4. Zet tijdelijk `SUPERFRIS_IMAP_PROBE_ON_STARTUP=true` en herstart de service. Controleer de Render-app-logregel `Superfris IMAP startup probe` (uitsluitend status en of de conceptenmap gevonden is). De probe verstuurt noch bewaart berichten. Controleer ook de Basic Auth-routes `GET /api/superfris/status` en `GET /api/superfris/imap-check`.
5. Nadat de IMAP-probe status `connected` en `drafts_folder_found=true` geeft,
   activeer `SUPERFRIS_ENABLED=true`. De bestaande Workspace 2
   `/health` wake start de gescheiden Superfris-verwerking, zonder nieuwe service
   of GitHub Actions Secret. Begin met een gecontroleerde eerste run en controleer
   het resultaat in STRATO Webmail.
6. Houd `SUPERFRIS_IMAP_PROBE_ON_STARTUP` daarna uit. De mailmodule
   maakt alleen concepten; er is geen automatische verzendfunctie.

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
  Amsterdamse kalenderdag op, met IMAP Draft-vlag in de daadwerkelijk gevonden conceptenmap.
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

## Status technische validatie

GitHub Actions kon eerder niet starten (job zonder runner en stappen); de Python-testuitvoering is nog niet via CI bevestigd. Laat de eerste Render-deploy met `SUPERFRIS_ENABLED=false` uitvoeren en controleer op import/startupfouten vóór activeren.
