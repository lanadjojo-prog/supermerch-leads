"""Safe boot for the new Render workspace.

Never start outreach against a temporary/empty SQLite database. Never unlock
automated mail before existing campaigns and encrypted Zoho tokens are restored.
This file is used only by Workspace 2, not by the old Leads service.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def missing_configuration():
    required = {
        "DATABASE_URL",
        "SESSION_SECRET",
        "APP_USERNAME",
        "APP_PASSWORD",
        "GOOGLE_PLACES_API_KEY",
        "ZOHO_CLIENT_ID",
        "ZOHO_CLIENT_SECRET",
        "ZOHO_FROM_ADDRESS",
    }
    missing = sorted(name for name in required if not os.environ.get(name, "").strip())
    db = os.environ.get("DATABASE_URL", "")
    if db and not db.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
        missing.append("DATABASE_URL (PostgreSQL required)")
    if not os.environ.get("LEADS_DATA_MIGRATED", "").lower() in ("1", "true", "yes"):
        missing.append("LEADS_DATA_MIGRATED (historical DB import pending)")
    return missing


class MigrationStatus(BaseHTTPRequestHandler):
    def do_GET(self):
        missing = missing_configuration()
        status = {
            "ok": False,
            "state": "awaiting_secure_database_and_credentials",
            "reason": "Safely waiting for existing lead data and encrypted Zoho mailbox to be migrated.",
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
