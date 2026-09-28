from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
import logging
import random
import time

from ..config import settings

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredCompany:
    name: str
    website: str
    address: str | None = None
    source: str = "google_places"


def normalize_domain(url: str) -> str:
    if not url:
        return ""
    candidate = url.strip()
    if not candidate.startswith(("http://", "https://")):
        candidate = "https://" + candidate
    host = (urlparse(candidate).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _post_places_with_retry(client: httpx.Client, endpoint: str, headers: dict, payload: dict) -> httpx.Response:
    """Retry transient Google/API/network failures instead of aborting the lead run immediately."""
    max_attempts = 5
    retryable_statuses = {429, 500, 502, 503, 504}

    for attempt in range(1, max_attempts + 1):
        try:
            response = client.post(endpoint, headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if attempt == max_attempts:
                raise RuntimeError(f"Google Places netwerkfout na {max_attempts} pogingen: {exc}") from exc
            delay = min(2 ** (attempt - 1), 16) + random.uniform(0, 0.5)
            logger.warning("Google Places netwerkfout (poging %s/%s); retry over %.1fs: %s", attempt, max_attempts, delay, exc)
            time.sleep(delay)
            continue

        if response.status_code not in retryable_statuses:
            return response

        if attempt == max_attempts:
            return response

        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else min(2 ** (attempt - 1), 16) + random.uniform(0, 0.5)
        except ValueError:
            delay = min(2 ** (attempt - 1), 16) + random.uniform(0, 0.5)
        logger.warning(
            "Google Places tijdelijk HTTP %s (poging %s/%s); retry over %.1fs",
            response.status_code,
            attempt,
            max_attempts,
            delay,
        )
        time.sleep(delay)

    raise RuntimeError("Google Places retry-loop onverwacht beëindigd.")


def discover_google_places(query: str, region: str, limit: int = 25) -> list[DiscoveredCompany]:
    if not settings.google_places_api_key:
        raise RuntimeError("GOOGLE_PLACES_API_KEY ontbreekt.")

    endpoint = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings.google_places_api_key,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.websiteUri,nextPageToken",
    }
    companies: list[DiscoveredCompany] = []
    seen: set[str] = set()
    page_token: str | None = None

    with httpx.Client(timeout=20, follow_redirects=True) as client:
        while len(companies) < limit:
            payload = {
                "textQuery": f"{query} {region}".strip(),
                "pageSize": min(20, max(1, limit - len(companies))),
                "languageCode": "nl",
                "regionCode": "NL",
            }
            if page_token:
                payload["pageToken"] = page_token

            response = _post_places_with_retry(client, endpoint, headers, payload)
            if response.status_code >= 400:
                try:
                    detail = response.json()
                except Exception:
                    detail = response.text[:2000]
                logger.error("Google Places error %s after retries: %s", response.status_code, detail)
                raise RuntimeError(
                    f"Google Places gaf HTTP {response.status_code} na retries. Controleer Places API (New), billing en API-keyrestricties."
                )

            data = response.json()
            for place in data.get("places", []):
                website = place.get("websiteUri") or ""
                domain = normalize_domain(website)
                if not domain or domain in seen:
                    continue
                seen.add(domain)
                companies.append(
                    DiscoveredCompany(
                        name=(place.get("displayName") or {}).get("text") or domain,
                        website=website,
                        address=place.get("formattedAddress"),
                    )
                )
                if len(companies) >= limit:
                    break
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    return companies
