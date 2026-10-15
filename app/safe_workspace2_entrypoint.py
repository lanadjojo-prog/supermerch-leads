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
    if any(key == "sslmode" for key, _ in params):
        return
    params.append(("sslmode", "require"))
    os.environ["DATABASE_URL"] = urlunsplit(parsed._replace(query=urlencode(params)))


def _safe_database_failure_reason(exc):
    """Return an actionable error category without exposing URI or credentials."""
    msg = str(exc).lower()
    if any(term in msg for term in ("could not translate host name", "name or service not known", "temporary failure in name resolution", "nodename nor servname provided")):
        return "hostname_not_resolvable_use_external_database_url"
    if any(term in msg for term in ("password authentication failed", "authentication failed for user")):
        return "database_password_or_user_rejected"
    if any(term in msg for term in ("connection timed out", "timeout expired", "operation timed out")):
        return "database_network_timeout_check_ip_allowlist"
    if any(term in msg for term in ("connection refused", "network is unreachable")):
        return "database_host_unreachable_check_external_url"
    if "no pg_hba.conf entry" in msg:
        return "database_access_rejected_check_ip_allowlist_and_tls"
    if any(term in msg for term in ("ssl", "tls", "certificate verify failed")):
        return "database_tls_negotiation_failed"
    return "database_connection_failed_check_external_url_and_access"


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
    try:
        import psycopg
        import base64
        import hashlib
        from cryptography.fernet import Fernet
        psql = url.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(psql, connect_timeout=6, options="-c statement_timeout=6000") as conn:
            with conn.cursor() as cur:
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
    from app.services.discovery import discover_google_places
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
        matches = discover_google_places("carnavalsvereniging", "Oosterhout", 1)
        print(
            f"LEADS_READINESS_PLACES_OK results={len(matches)} emails_sent=0",
            flush=True,
        )
    except Exception as exc:
        print(f"LEADS_READINESS_PLACES_FAILED category={type(exc).__name__} emails_sent=0", flush=True)
    print(
        "LEADS_READINESS_AI_CONFIGURED=" + str(bool(settings.openai_api_key))
        + " emails_sent=0",
        flush=True,
    )


def main():
    _require_postgres_tls()
    missing = missing_configuration()
    if missing:
        print("Workspace 2 pending secure lead-data migration:", ", ".join(missing), flush=True)
        port = int(os.environ.get("PORT", "10000"))
        ThreadingHTTPServer(("0.0.0.0", port), MigrationStatus).serve_forever()
        return
    outreach_enabled = os.environ.get("AUTOMATION_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
    if outreach_enabled and os.environ.get("LEADS_GO_LIVE_APPROVED", "").strip().lower() not in ("1", "true", "yes"):
        print("Refusing to enable outreach without explicit migration completion approval.", flush=True)
        sys.exit(1)
    if os.environ.get("ZOHO_DRY_RUN_CHECK", "").strip().lower() in ("1", "true", "yes"):
        _non_sending_zoho_diagnostic()
    if os.environ.get("LEADS_READINESS_CHECK", "").strip().lower() in ("1", "true", "yes"):
        _lead_readiness_diagnostic()
    print("Workspace 2 prerequisites present. Starting app; outreach_enabled=" + str(outreach_enabled), flush=True)
    port = os.environ.get("PORT", "10000")
    os.execvp("gunicorn", [
        "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "1",
        "-b", "0.0.0.0:" + port, "app.main:app",
    ])


if __name__ == "__main__":
    main()
