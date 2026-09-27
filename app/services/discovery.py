from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
import logging

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
            response = client.post(endpoint, headers=headers, json=payload)
            if response.status_code >= 400:
                try:
                    detail = response.json()
                except Exception:
                    detail = response.text[:2000]
                logger.error("Google Places error %s: %s", response.status_code, detail)
                raise RuntimeError(f"Google Places gaf HTTP {response.status_code}. Controleer Places API (New), billing en API-keyrestricties.")
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
