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

## Instagram prospecting

`/instagram/` shows discovered accounts, source websites, API action outcomes and
recent runs. Discovery reuses the existing public-site crawler for up to five
qualified SuperMerch leads per hour, between 09:00 and 19:00 Europe/Amsterdam.
It finds explicit profile links on company websites; it does not scrape the
Instagram feed or guess usernames. Discovery requires existing qualified leads.
The existing health wake and in-process scheduler trigger the worker; unique DB
claims suppress duplicate runs across processes. Failed site scans are retained
for operator inspection, not retried in a tight loop.

Set these variables in the service's secret environment (never in Git):

- `INSTAGRAM_CRAWLER_ENABLED=true`: enable account discovery.
- `INSTAGRAM_USER_ID`: the numeric Instagram professional-account ID.
- `INSTAGRAM_GRAPH_VERSION`: the supported Graph API version selected for the Meta app.
- `INSTAGRAM_ACCESS_TOKEN`: a Meta access token with Business Discovery access and
  `instagram_basic` / `instagram_manage_engagement`, plus any required Facebook
  Login permissions. Configure the app/account with Meta before enabling likes.
- `INSTAGRAM_LIKES_ENABLED=true`: enable API likes after configuring and verifying
  the Meta integration. Browser login does not provide these API credentials.

The worker verifies the connected username is **supermerch.nl** before liking.
It processes at most one account per hour, two public recent posts per account,
and ten attempts per Dutch calendar day. Posts older than 30 days are skipped.
Professional-account Business Discovery limits apply; unsupported accounts may
cause the API to reject a request. There are no DMs, follows, browser-cookie
exports, private endpoints or anti-bot bypasses.

Each post and daily slot is reserved durably BEFORE the API request. Crashed or
unconfirmed requests consume budget and are not automatically retried. Any API
failure pauses further engagement via `chat_commands.command_id =
instagram-engagement-paused`. An operator must resolve the cause and explicitly
remove this pause record to resume. Never remove action reservations to retry an
uncertain request without checking its result. Account processing claims are
also permanent to prevent repeated engagement after restarts.

Limitations: candidate qualification inherits the lead score, not a review of
individual post text. An automatic like is not evidence of purchase intent.
The free Render service may sleep; scheduled hourly wake-ups are best-effort,
not a guaranteed execution time. No new paid services are introduced.

API references:
- https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/user-likes
- https://developers.facebook.com/blog/post/2026/04/22/instagram-api-updates-for-partnerships-metrics-collaboration-and-engagement/

Validation: `python -m pytest tests/test_instagram_engine.py -q`. Live Meta calls
must still be tested after account permission/configuration; unit tests use a
fake API and do not claim access to the production Instagram account.
