"""Independent Render cron wake-up for the SuperMerch draft-only lead engine.

Keep GitHub Actions completely out of the critical path. The web application
performs Amsterdam-hour slot deduplication and enforces the daily draft cap.
Never send mail, and never include authentication secrets in this wakeup.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


TARGET_URL = "https://supermerch-leads-ws2.onrender.com/health"


def main() -> int:
    local = datetime.now(ZoneInfo("Europe/Amsterdam"))
    if not 9 <= local.hour < 19:
        print(f"Outside Amsterdam lead window ({local.isoformat()}), no wake needed.", flush=True)
        return 0

    for attempt in range(1, 4):
        try:
            req = Request(
                TARGET_URL,
                headers={"User-Agent": "SuperMerch-Render-Cron-Wakeup/1.0"},
                method="GET",
            )
            with urlopen(req, timeout=190) as response:
                body = response.read(65536).decode("utf-8")
                data = json.loads(body)
                if response.status == 200 and data.get("ok") is True and data.get("automation_triggered") is True:
                    print(f"Wake successful, Amsterdam={local.isoformat()}, attempt={attempt}, automated_email_sent=0", flush=True)
                    return 0
                print(f"Wake returned unexpected status/body on attempt {attempt}", flush=True)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            print(f"Wake failed, attempt={attempt}, error_type={type(exc).__name__}", flush=True)
        if attempt < 3:
            time.sleep(15 * attempt)

    print("Lead wake failed after retries; Render should alert on failed cron run.", file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
