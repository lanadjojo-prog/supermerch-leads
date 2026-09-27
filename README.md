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
