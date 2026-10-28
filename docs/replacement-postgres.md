# Replacement PostgreSQL after Render expiration

The Workspace 2 service still used the original Workspace 1 database. That
database expired on 2026-10-27. A new database does not restore its campaigns,
leads, deduplication history, or encrypted Zoho mailbox authorization.

Use a persistent PostgreSQL provider. Configure its TLS connection string as
`DATABASE_URL` in Render, preserving `SESSION_SECRET` for any future restore.
Never commit either value. Do not use local SQLite on a free Render instance.

## First boot with empty replacement storage

Set `DATABASE_BOOTSTRAP_MODE=fresh_discovery` and explicitly set all these
variables to `false`:

```text
AUTOMATION_ENABLED
AUTOMATED_OUTREACH_ENABLED
INSTAGRAM_LIKES_ENABLED
SUPERFRIS_ENABLED
ZOHO_SELF_TEST
ZOHO_DRAFT_SELF_TEST
SUPERFRIS_DRAFT_SELFTEST_ON_STARTUP
```

This mode verifies a real TLS PostgreSQL connection. Normal application startup
creates missing tables without dropping or replacing data. The dashboard and
manual campaign discovery can work without the old mailbox token. Only a
`start_campaign` command may run at startup; mailbox commands and catch-up jobs
are not replayed. Automatic mail and likes remain
disabled because their previous reservations are unavailable.

If a backup becomes available, restore into separately verified storage and
check counts and the stored mailbox token before switching. Keep the expired
source configuration available securely; do not delete the old database.

## Resume automation

Reconnect Zoho through the application's integrations page if its encrypted
authorization could not be restored. Reconcile existing drafts/contact history
before resuming mail processing. Only after that reconciliation, change
`DATABASE_BOOTSTRAP_MODE` back to `existing`. That default mode still requires
all original application tables and a decryptable Zoho authorization.

Instagram discovery does not require a Meta token. Actual API likes require
`INSTAGRAM_USER_ID`, `INSTAGRAM_GRAPH_VERSION`, and `INSTAGRAM_ACCESS_TOKEN`,
with the correct Meta permissions. Verify @supermerch.nl and a real API request
before enabling `INSTAGRAM_LIKES_ENABLED`. Local mocked tests do not establish
live Meta authorization.

## Validation

```bash
python -m pytest tests/test_instagram_engine.py tests/test_replacement_database.py -q
```

These tests cover account selection, daily caps, duplicate prevention, ambiguous
API results, TLS enforcement, and refusal to bootstrap with automatic mail or
likes enabled. Live replacement storage and Meta access still require separate
verification.
