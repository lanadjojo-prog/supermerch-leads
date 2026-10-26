# SuperMerch Lead Engine

MVP voor het automatisch vinden, analyseren en prioriteren van B2B-leads voor SuperMerch.

## V1
- campagne aanmaken op niche, regio en zoekopdracht;
- bedrijven vinden via Google Places Text Search;
- websites crawlen;
- vacatures, employer branding, groei, events en merchandise-signalen analyseren;
- transparante leadscore berekenen;
- openbare e-mailadressen verzamelen;
- concept-outreach maken;
- leads goedkeuren of afwijzen in het dashboard;
- dagelijks automatisch nieuwe bedrijven zoeken over vaste SuperMerch-categorieën;
- maximaal 30 succesvolle Zoho-mails per Nederlandse kalenderdag versturen, in kleine batches;
- succesvolle verzendingen apart loggen zodat retries nooit opnieuw 30 mails starten.

## Environment variables
- APP_USERNAME
- APP_PASSWORD
- DATABASE_URL
- GOOGLE_PLACES_API_KEY
- OPENAI_API_KEY (optioneel; zonder key gebruikt de app heuristieken)
- OPENAI_MODEL

De automatische worker gebruikt alleen gekwalificeerde leads met een openbaar gevonden zakelijk e-mailadres en een gegenereerde outreachmail. Mislukte of overgeslagen leads tellen niet mee voor het dagdoel.

## Dagelijkse automation

De webservice voert tussen 09:00 en 19:00 Europe/Amsterdam maximaal één automation-run per uur uit.
Elke run verstuurt maximaal 5 succesvolle mails en controleert altijd eerst de dagteller.
Een GitHub Actions health-ping houdt de gratis Render-webservice alleen tijdens het dagvenster wakker.


## Workspace 2 — dual-brand drafts (2026-10-26)

Both campaigns run in the existing Workspace 2 web service. Email is NEVER
automatically sent: set AUTOMATED_OUTREACH_ENABLED=false.

- SuperMerch: new public-company candidates -> checked first-party websites
  and email sources -> opportunity score -> *review-only* Zoho drafts,
  max 30 attempts/day, up to 5 per pass.
- Superfris: separate B2B candidates -> first-party website contacts ->
  conservative design-first mail copy -> gio@superfris.online STRATO IMAP
  Drafts folder, max 30 attempts/day, up to 5 per pass.
- Both brands use LEADS_FREE_MODE=true (default). This disables calls to
  Google Places and OpenAI for discovery, analysis and copywriting. Using
  LEADS_FREE_MODE=false opts back into separately billed OpenAI API usage.
- Free business discovery uses modest, bounded, cached OpenStreetMap searches
  via public Overpass. Attribution: © OpenStreetMap contributors (ODbL);
  https://www.openstreetmap.org/copyright . The free Overpass instance is a
  shared, rate-limited, best-effort service with no uptime guarantee. Do not
  use it as a guaranteed commercial lead feed; a paid provider, licensed dataset
  or self-hosted extracts are needed for predictable high volume.
- Addresses are never generated or guessed; only public emails found on a
  company-controlled page qualify. Duplicate company domains are blocked.
- A general sector fit does NOT imply buying intent. Fit-only drafts contain
  no assertions about purchases, events, deadlines or previous contact.
- Thirty per brand is a CAP/TARGET, not a promise of 30 suitable sourced
  companies every day. Actual draft counts are visible on the shared dashboard.
- AUTOMATION_ENABLED controls research schedules. SUPERFRIS_ENABLED=true and
  SUPERFRIS_STRATO_PASSWORD are needed for STRATO IMAP draft saving.
- Public /health can wake both scheduled workers; no secret is exposed.
