"""Safe boot for the new Render workspace.

Never start outreach against a temporary/empty SQLite database. Never unlock
automated mail before existing campaigns and encrypted Zoho tokens are restored.
This file is used only by Workspace 2, not by the old Leads service.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _require_postgres_tls():
    """Default cross-workspace PostgreSQL connections to encrypted transport.

    The database URL is stored in Render's secret environment; never echo it.
    Keep an explicit secure sslmode unchanged and never override an explicit
    insecure configuration.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
        return
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parsed = urlsplit(url)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key == "sslmode" for key, _ in params):
        params.append(("sslmode", "require"))
    # Render's IPv6 route is unavailable. Preserve the hostname for TLS/SNI,
    # while letting libpq connect over a freshly resolved IPv4 address.
    if (parsed.hostname or "").endswith(".neon.tech") and not any(key == "hostaddr" for key, _ in params):
        import socket
        try:
            addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 5432,
                                           family=socket.AF_INET, type=socket.SOCK_STREAM)
            if addresses:
                params.append(("hostaddr", addresses[0][4][0]))
        except OSError:
            pass  # Normal connection handling reports a sanitized error.
    os.environ["DATABASE_URL"] = urlunsplit(parsed._replace(query=urlencode(params)))


def _safe_database_failure_reason(exc):
    """Return an actionable error category without exposing URI or credentials."""
    msg = str(exc).lower()
    if any(term in msg for term in ("could not translate host name", "name or service not known", "temporary failure in name resolution", "nodename nor servname provided")):
        return "hostname_not_resolvable_use_external_database_url"
    if any(term in msg for term in ("password authentication failed", "authentication failed for user")):
        return "database_password_or_user_rejected"
    for term, category in (
        ("channel binding", "database_channel_binding_error"),
        ("unsupported startup parameter", "database_unsupported_startup_parameter"),
        ("invalid connection option", "database_invalid_connection_option"),
        ("endpoint id", "database_endpoint_routing_error"),
        ("does not exist", "database_or_role_missing"),
        ("timeout", "database_connection_timeout"),
    ):
        if term in msg:
            return category
    if any(term in msg for term in ("connection timed out", "timeout expired", "operation timed out")):
        return "database_network_timeout_check_ip_allowlist"
    if "connection refused" in msg:
        return "database_tcp_connection_refused"
    if "network is unreachable" in msg:
        return "database_network_unreachable"
    if "no pg_hba.conf entry" in msg:
        return "database_access_rejected_check_ip_allowlist_and_tls"
    if any(term in msg for term in ("ssl", "tls", "certificate verify failed")):
        return "database_tls_negotiation_failed"
    return "database_connection_failed_check_external_url_and_access"


def _database_network_diagnostic(url):
    """Report only address families and socket results, never credentials."""
    import socket
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 5432,
                                       type=socket.SOCK_STREAM)
        results = []
        for family, socktype, proto, _, address in addresses:
            with socket.socket(family, socktype, proto) as probe:
                probe.settimeout(2)
                code = probe.connect_ex(address)
            results.append({"family": "ipv4" if family == socket.AF_INET else "ipv6",
                            "socket_result": code})
        print("DATABASE_NETWORK_DIAGNOSTIC", json.dumps(results), flush=True)
    except OSError as exc:
        print("DATABASE_NETWORK_DIAGNOSTIC", type(exc).__name__, "errno=" + str(exc.errno), flush=True)


def _existing_database_preflight():
    """Require the *existing* lead data and decryptable Zoho link before boot.

    Cross-workspace Render Postgres requires the external TLS URL. A new/empty
    database is not a suitable substitute, because it loses mail deduplication.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return "DATABASE_URL (existing database URL missing)"
    if not url.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
        return "DATABASE_URL (PostgreSQL URL required)"
    from urllib.parse import urlsplit, parse_qsl
    params = dict(parse_qsl(urlsplit(url).query))
    if params.get("sslmode") not in ("require", "verify-ca", "verify-full"):
        return "DATABASE_URL (external connection must use sslmode=require)"
    mode = os.environ.get("DATABASE_BOOTSTRAP_MODE", "existing").strip().lower()
    replacement_modes = ("fresh_discovery", "instagram_only")
    if mode not in ("existing", *replacement_modes):
        return "DATABASE_BOOTSTRAP_MODE (use existing, fresh_discovery or instagram_only)"
    if mode in replacement_modes:
        # A replacement DB has neither history nor a stored mailbox. Allow the
        # dashboard and manual discovery, but never replay old mail work.
        disabled = ["AUTOMATED_OUTREACH_ENABLED",
                     "ZOHO_SELF_TEST", "ZOHO_DRAFT_SELF_TEST"]
        # Superfris has its own STRATO IMAP draft-only pipeline. It does not
        # use Zoho, transmit email, or replay any prior SuperMerch campaigns.
        # Permit draft-only discovery for both brands in fresh mode. The
        # optional STRATO self-test writes only to the linked owner's own
        # Drafts folder, never sends. Zoho API saves require fresh OAuth.
        # Automatic email sending remains prohibited.
        if mode == "fresh_discovery":
            disabled.append("INSTAGRAM_LIKES_ENABLED")
        for name in disabled:
            if os.environ.get(name, "").strip().lower() not in ("0", "false", "no", "off"):
                return f"{name} (must be explicitly false for fresh_discovery)"
    try:
        import psycopg
        import base64
        import hashlib
        from cryptography.fernet import Fernet
        psql = url.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(psql, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                # PgBouncer rejects the options startup parameter. A transaction
                # scoped timeout works for both direct and pooled connections.
                cur.execute("SET LOCAL statement_timeout = '6s'")
                if mode in replacement_modes:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                    return ""
                cur.execute("""SELECT to_regclass('public.campaigns'),
                                      to_regclass('public.leads'),
                                      to_regclass('public.chat_commands'),
                                      to_regclass('public.mail_integrations')""")
                if not all(cur.fetchone()):
                    return "DATABASE_URL (existing lead tables missing)"
                cur.execute("SELECT refresh_token_encrypted FROM mail_integrations WHERE provider = 'zoho' LIMIT 1")
                record = cur.fetchone()
                if not record or not record[0]:
                    return "ZOHO_MAIL (existing connected mailbox missing)"
                secret = os.environ.get("SESSION_SECRET", "").encode("utf-8")
                encryption_key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
                try:
                    Fernet(encryption_key).decrypt(record[0].encode("utf-8"))
                except Exception:
                    return "SESSION_SECRET (does not decrypt existing Zoho connection)"
    except Exception as exc:
        # Never expose passwords, URLs, SQL or provider tokens in logs/HTTP responses.
        print("Workspace 2 DB preflight failed:", type(exc).__name__, _safe_database_failure_reason(exc), flush=True)
        if os.environ.get("DATABASE_NETWORK_DIAGNOSTICS", "false").lower() == "true":
            _database_network_diagnostic(url)
        return "DATABASE_URL (connection unavailable: check external URL, TLS and IP allowlist)"
    return ""


def missing_configuration():
    required = {
        "DATABASE_URL", "SESSION_SECRET", "APP_USERNAME",
        "APP_PASSWORD", "GOOGLE_PLACES_API_KEY",
        "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_FROM_ADDRESS",
    }
    missing = sorted(name for name in required if not os.environ.get(name, "").strip())
    if not missing:
        db_error = _existing_database_preflight()
        if db_error:
            missing.append(db_error)
    return missing

class MigrationStatus(BaseHTTPRequestHandler):
    def do_GET(self):
        missing = missing_configuration()
        status = {
            "ok": False,
            "state": "awaiting_existing_database_connection",
            "reason": "Awaiting an authenticated connection to the existing lead database and Zoho mailbox.",
            "missing_configuration": missing,
            "automation_running": False,
        }
        raw = json.dumps(status, ensure_ascii=False).encode()
        self.send_response(503)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        return


def _non_sending_zoho_diagnostic():
    """Read-only mailbox and OAuth probe. Never calls the send-email endpoint.

    Enabled only on demand via ZOHO_DRY_RUN_CHECK for migration testing.
    Do not log tokens, mail addresses, full API responses or database URLs.
    """
    from sqlalchemy import select
    from app.database import SessionLocal
    from app.models import MailIntegration
    from app.services import zoho_mail

    db = SessionLocal()
    try:
        row = db.scalar(select(MailIntegration).where(MailIntegration.provider == "zoho"))
        if row is None:
            print("ZOHO_DRY_RUN_FAILED reason=stored_mailbox_missing emails_sent=0", flush=True)
            return
        token = zoho_mail.refresh_access_token(zoho_mail._decrypt(row.refresh_token_encrypted))
        accounts = zoho_mail.get_accounts(token)
        found = any(
            zoho_mail._account_id(account) == str(row.account_id)
            for account in accounts if isinstance(account, dict)
        )
        result = "OK" if found else "FAILED"
        print(
            f"ZOHO_DRY_RUN_{result} oauth_refresh=ok account_read=ok "
            f"linked_account_found={found} emails_sent=0",
            flush=True,
        )
    except Exception as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        category = (
            "oauth_or_account_http_failure" if status else
            "connection_or_authorization_failure"
        )
        print(
            f"ZOHO_DRY_RUN_FAILED category={category} "
            f"http_status={status if isinstance(status, int) else 'none'} "
            "emails_sent=0",
            flush=True,
        )
    finally:
        db.close()


def _lead_readiness_diagnostic():
    """Non-sending health check of deduplication, lead inventory and Places API.

    Does not alter leads/campaigns and never invokes an email send method.
    """
    from sqlalchemy import func, select
    from app.database import SessionLocal
    from app.models import Campaign, Lead, ChatCommand
    from app.services.openai_discovery import discover_openai
    from app.config import settings

    db = SessionLocal()
    try:
        rows = db.execute(
            select(Lead.status, func.count(Lead.id)).group_by(Lead.status)
        ).all()
        statuses = {str(status): int(n) for status, n in rows}
        campaigns = int(db.scalar(select(func.count(Campaign.id))) or 0)
        sends_logged = int(db.scalar(select(func.count(ChatCommand.id)).where(
            ChatCommand.action == "outreach_send",
            ChatCommand.status == "complete"
        )) or 0)
        eligible = int(db.scalar(select(func.count(Lead.id)).where(
            Lead.status == "approved",
            Lead.email.is_not(None),
            Lead.outreach_text.is_not(None)
        )) or 0)
        print(
            "LEADS_READINESS_DB_OK "
            f"campaigns={campaigns} total_leads={sum(statuses.values())} "
            f"approved_with_email_and_draft={eligible} "
            f"review_pending={statuses.get('ready_for_review', 0)} "
            f"contacted={statuses.get('contacted', 0)} "
            f"send_history={sends_logged} emails_sent=0",
            flush=True,
        )
    except Exception as exc:
        print(f"LEADS_READINESS_DB_FAILED category={type(exc).__name__} emails_sent=0", flush=True)
    finally:
        db.close()

    try:
        matches = discover_openai("carnavalsvereniging", "Oosterhout", 1)
        print(
            f"LEADS_READINESS_SEARCH_OK results={len(matches)} emails_sent=0",
            flush=True,
        )
    except Exception as exc:
        print(f"LEADS_READINESS_SEARCH_FAILED category={type(exc).__name__} emails_sent=0", flush=True)
    print(
        "LEADS_READINESS_AI_CONFIGURED=" + str(bool(settings.openai_api_key))
        + " emails_sent=0",
        flush=True,
    )


def _zoho_send_self_test():
    """One-time send test ONLY to the Zoho account's own address.

    A durable claim is recorded before calling Zoho so a cold-start retry
    cannot accidentally send a second message.
    """
    from datetime import datetime
    from sqlalchemy import select
    from app.database import SessionLocal
    from app.models import ChatCommand, MailIntegration
    from app.services import zoho_mail

    db = SessionLocal()
    key = "zoho-self-test-ws2-2026-10-15-v1"
    try:
        if db.scalar(select(ChatCommand.id).where(ChatCommand.command_id == key)):
            print("ZOHO_SELFTEST_SKIPPED already_attempted=True", flush=True)
            return
        row = db.scalar(select(MailIntegration).where(MailIntegration.provider == "zoho"))
        if row is None or not row.email_address:
            print("ZOHO_SELFTEST_FAILED category=own_mailbox_missing", flush=True)
            return
        job = ChatCommand(command_id=key, action="zoho_self_test", status="running")
        db.add(job)
        db.commit()
        try:
            zoho_mail.send_email(
                db,
                to_address=row.email_address,
                subject="SuperMerch Leadmachine - technische verzendtest",
                content=(
                    "Dit is een eenmalige interne verzendtest vanuit de herstelde "
                    "SuperMerch Leadmachine. Er is geen acquisitiemail naar een "
                    "externe lead verzonden."
                ),
            )
            job.status = "complete"
            job.finished_at = datetime.utcnow()
            db.commit()
            print("ZOHO_SELFTEST_OK sent_to=linked_mailbox external_leads_contacted=0", flush=True)
        except Exception as exc:
            db.rollback()
            job = db.scalar(select(ChatCommand).where(ChatCommand.command_id == key))
            if job:
                job.status = "error"
                job.finished_at = datetime.utcnow()
                db.commit()
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            print(
                f"ZOHO_SELFTEST_FAILED category={type(exc).__name__} "
                f"http_status={status if isinstance(status, int) else 'none'} "
                "external_leads_contacted=0",
                flush=True,
            )
    except Exception as exc:
        db.rollback()
        print(
            f"ZOHO_SELFTEST_PRECHECK_FAILED category={type(exc).__name__} "
            "external_leads_contacted=0",
            flush=True,
        )
    finally:
        db.close()


def _zoho_draft_self_test():
    """Save precisely one internal draft to the linked mailbox. No mail is sent."""
    from datetime import datetime
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from app.database import SessionLocal
    from app.models import ChatCommand, MailIntegration
    from app.services import zoho_mail

    db = SessionLocal()
    key = "zoho-draft-selftest-workspace2-v1"
    try:
        if db.scalar(select(ChatCommand.id).where(ChatCommand.command_id == key)):
            print("ZOHO_DRAFT_SELFTEST_SKIPPED duplicate_prevented=True", flush=True)
            return
        mailbox = db.scalar(select(MailIntegration).where(MailIntegration.provider == "zoho"))
        if not mailbox:
            print("ZOHO_DRAFT_SELFTEST_FAILED no_linked_mailbox=True", flush=True)
            return
        claim = ChatCommand(command_id=key, action="zoho_draft_selftest", status="running")
        db.add(claim)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            print("ZOHO_DRAFT_SELFTEST_SKIPPED duplicate_prevented=True", flush=True)
            return
        try:
            zoho_mail.save_draft(
                db,
                to_address=mailbox.email_address,
                subject="SuperMerch - test van Zoho-concepten (niet versturen)",
                content=(
                    "Dit is een testconcept. De leadmachine heeft dit als concept "
                    "opgeslagen, zonder een bericht te versturen. "
                    "Vanaf morgen worden nieuwe bedrijven in concepten voorbereid."
                ),
            )
            claim.status = "complete"
            claim.finished_at = datetime.utcnow()
            db.commit()
            print("ZOHO_DRAFT_SELFTEST_OK saved_to_drafts=True emails_sent=0", flush=True)
        except Exception as exc:
            db.rollback()
            claim = db.scalar(select(ChatCommand).where(ChatCommand.command_id == key))
            if claim:
                claim.status = "error"
                claim.finished_at = datetime.utcnow()
                db.commit()
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            print(
                f"ZOHO_DRAFT_SELFTEST_FAILED error_type={type(exc).__name__} "
                f"http_status={status if isinstance(status, int) else 'none'} "
                "emails_sent=0",
                flush=True,
            )
    finally:
        db.close()


def main():
    _require_postgres_tls()
    missing = missing_configuration()
    if missing:
        print("Workspace 2 pending secure lead-data migration:", ", ".join(missing), flush=True)
        port = int(os.environ.get("PORT", "10000"))
        ThreadingHTTPServer(("0.0.0.0", port), MigrationStatus).serve_forever()
        return
    discovery_enabled = os.environ.get("AUTOMATION_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
    outreach_enabled = discovery_enabled and os.environ.get("AUTOMATED_OUTREACH_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
    if outreach_enabled and os.environ.get("LEADS_GO_LIVE_APPROVED", "").strip().lower() not in ("1", "true", "yes"):
        print("Refusing to enable outreach without explicit migration completion approval.", flush=True)
        sys.exit(1)
    if os.environ.get("ZOHO_DRY_RUN_CHECK", "").strip().lower() in ("1", "true", "yes"):
        _non_sending_zoho_diagnostic()
    if os.environ.get("DATABASE_BOOTSTRAP_MODE", "existing").strip().lower() in ("fresh_discovery", "instagram_only"):
        print("Replacement PostgreSQL connected; previous history not restored; automatic mail disabled.", flush=True)
        os.environ.pop("LEAD_CATCHUP_ON_STARTUP_DATE", None)
    elif os.environ.get("LEADS_READINESS_CHECK", "").strip().lower() in ("1", "true", "yes"):
        _lead_readiness_diagnostic()
    if os.environ.get("ZOHO_SELF_TEST", "").strip().lower() in ("1", "true", "yes"):
        _zoho_send_self_test()
    if os.environ.get("ZOHO_DRAFT_SELF_TEST", "").strip().lower() in ("1", "true", "yes"):
        _zoho_draft_self_test()
    if os.environ.get("INSTAGRAM_SMOKE_TEST_ON_STARTUP", "false").lower() == "true":
        from app.instagram_selftest import run_selftests
        if not run_selftests():
            print("INSTAGRAM_STARTUP_ABORTED: smoke tests failed; no external likes or mail sent.", flush=True)
            sys.exit(1)
    if os.environ.get("INSTAGRAM_READONLY_CHECK_ON_STARTUP", "false").lower() == "true":
        # An authenticated GET against Meta and a public-company website read:
        # never call MetaClient.like(), never send messages, never write leads.
        from app.services.instagram_engine import MetaClient, connection_ready
        from app.services.crawler import crawl_website
        if connection_ready():
            client = None
            try:
                client = MetaClient()
                token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
                token_family = ("instagram_login" if token.startswith("IG")
                    else "facebook_login" if token.startswith("EAA") else "unknown")
                print(f"INSTAGRAM_META_TOKEN_FAMILY {token_family} likes=0", flush=True)
                try:
                    me = client.request("GET", "me", params={"fields": "id"})
                    print(f"INSTAGRAM_META_SELF_OK same_as_configured_ig={str(me.get('id')) == client.user} likes=0", flush=True)
                except Exception as exc:
                    print(f"INSTAGRAM_META_SELF_FAIL code={getattr(exc, 'code', 'unknown')} likes=0", flush=True)
                # Diagnose OAuth scopes and Page connection without disclosing
                # account identifiers, tokens, emails or profile data.
                from app.services.instagram_engine import MetaError
                try:
                    permission_data = client.request("GET", "me/permissions")
                    permissions = permission_data.get("data", [])
                    granted = {
                        str(item.get("permission", "")).lower()
                        for item in permissions
                        if isinstance(item, dict) and item.get("status") == "granted"
                    }
                    required = ("instagram_basic", "instagram_manage_engagement",
                                "pages_show_list", "pages_read_engagement")
                    print("INSTAGRAM_META_PERMISSIONS " +
                          " ".join(f"{name}={name in granted}" for name in required) +
                          f" returned={len(permissions)} likes=0", flush=True)
                except MetaError as exc:
                    print(f"INSTAGRAM_META_PERMISSIONS_UNAVAILABLE code={exc.code} likes=0", flush=True)
                try:
                    page_data = client.request("GET", "me/accounts",
                        params={"fields": "id,instagram_business_account{id,username}", "limit": 100})
                    pages = page_data.get("data", [])
                    linked = [p.get("instagram_business_account", {}) for p in pages
                              if isinstance(p, dict)]
                    matches = sum(str(p.get("id", "")) == client.user for p in linked)
                    print(f"INSTAGRAM_META_PAGES pages={len(pages)} ig_link_matches={matches} likes=0", flush=True)
                except MetaError as exc:
                    print(f"INSTAGRAM_META_PAGES_UNAVAILABLE code={exc.code} likes=0", flush=True)
                client.verify()
                print("INSTAGRAM_META_ACCOUNT_VERIFIED likes=0", flush=True)
                media = client.media("stichting_kielegat")
                print(f"INSTAGRAM_META_READONLY_OK account_verified=1 media_visible={len(media)} likes=0", flush=True)
            except Exception as exc:
                # MetaClient wraps Graph API responses into a credential-free
                # status and numeric error code; never print raw responses.
                from app.services.instagram_engine import MetaError
                if isinstance(exc, MetaError):
                    print(f"INSTAGRAM_META_READONLY_FAIL code={exc.code} subcode={exc.subcode} scope_hint={exc.scope_hint} reason={str(exc)} likes=0", flush=True)
                else:
                    print(f"INSTAGRAM_META_READONLY_FAIL category={type(exc).__name__} likes=0", flush=True)
            finally:
                if client:
                    client.close()
        else:
            print("INSTAGRAM_META_READONLY_UNAVAILABLE credentials_missing_or_invalid_format likes=0", flush=True)
        try:
            website = crawl_website("https://web.kielegat.nl/evenement/elluf-elluf-2026/")
            print(f"INSTAGRAM_WEBSITE_READONLY pages={len(website.pages)} handles={len(website.instagram_profiles)} likes=0", flush=True)
        except Exception as exc:
            print(f"INSTAGRAM_WEBSITE_READONLY_FAIL category={type(exc).__name__}", flush=True)
    print("Workspace 2 prerequisites present. Starting app; discovery_enabled=" + str(discovery_enabled) + "; outreach_enabled=" + str(outreach_enabled), flush=True)
    port = os.environ.get("PORT", "10000")
    os.execvp("gunicorn", [
        "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "1",
        "-b", "0.0.0.0:" + port, "app.main:app",
    ])


if __name__ == "__main__":
    main()
