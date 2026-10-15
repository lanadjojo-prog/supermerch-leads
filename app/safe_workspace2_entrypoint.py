"""Safe boot for the new Render workspace.

Never start outreach against a temporary/empty SQLite database. Never unlock
automated mail before existing campaigns and encrypted Zoho tokens are restored.
This file is used only by Workspace 2, not by the old Leads service.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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
        print("Workspace 2 DB preflight failed:", type(exc).__name__, flush=True)
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


def main():
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
    print("Workspace 2 prerequisites present. Starting app; outreach_enabled=" + str(outreach_enabled), flush=True)
    port = os.environ.get("PORT", "10000")
    os.execvp("gunicorn", [
        "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "1",
        "-b", "0.0.0.0:" + port, "app.main:app",
    ])


if __name__ == "__main__":
    main()
