# SuperMerch Instagram prospecting

This module extends the existing Lead Engine. It is **disabled by default**, and it
does not log into Instagram via a browser, scrape authenticated pages, follow people
or send unsolicited DMs.

## Discovery flow

1. Select qualified leads with score >= 50 from the existing database.
2. Read each company homepage and extract only a direct Instagram profile URL.
3. Display the verified company/profile mapping on `/instagram`.
4. When configured, use Meta Business Discovery to fetch recent media from
   public professional (Business/Creator) accounts.
5. Select suitable recent posts at random, up to 2 per account and 10 total/day.
   Keep a unique media ID action log; pause on authentication or rate limit errors.

## Enable scanning

Set `INSTAGRAM_SCAN_ENABLED=true` in the correct Render workspace. Existing
health pings trigger the daily scan at most once after 09:00 Amsterdam time.
You can also scan manually from `/instagram` without turning on the scheduler.

## Enable likes later

Only after the Meta application is reviewed and connected to SuperMerch:
- Connect an Instagram Business or Creator account linked to a Facebook Page.
- Configure the official Facebook Login / Instagram Graph API.
- Obtain the required `instagram_manage_engagement` permission (and its dependencies)
  with appropriate Advanced Access / Meta App Review for the use case.
- Set `INSTAGRAM_IG_USER_ID` to your own IG User ID.
- Set `INSTAGRAM_ACCESS_TOKEN` as a secret on Render. Never commit tokens.
- Optionally set `INSTAGRAM_GRAPH_VERSION` (default `v25.0`).
- Explicitly enable `INSTAGRAM_LIKES_ENABLED=true`.

If API permissions are not approved, prospect research still works; likes remain
disabled. API cannot discover or engage with private profiles in this flow.

## Safeguards

No automated DMs, no follower automation, no usernames guessed from company names,
no Instagram login-cookie storage, no retries for already-attempted media IDs.
Post likes are never enabled just by a user logging into instagram.com.
Use only with Meta-authorized permissions and in line with platform rules.
