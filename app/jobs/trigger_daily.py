from __future__ import annotations

import os

import httpx


def main() -> None:
    url = os.getenv(
        "DAILY_JOB_URL",
        "https://supermerch-leads.onrender.com/api/internal/daily-lead-engine",
    )
    token = os.environ["DAILY_JOB_TOKEN"]
    response = httpx.post(
        url,
        headers={"X-Automation-Token": token},
        timeout=3300,
    )
    print(response.status_code)
    print(response.text)
    response.raise_for_status()


if __name__ == "__main__":
    main()
