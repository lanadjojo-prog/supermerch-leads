"""Low-cost public business discovery using modest, cached OpenStreetMap searches.

OpenStreetMap contributors (ODbL): https://www.openstreetmap.org/copyright
Public Overpass has no SLA. Respect rate limits and do not fabricate company data.
This module never creates emails, never sends mail and does not require API keys.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import re
import threading
import time
from urllib.parse import urlsplit

import httpx

from ..config import settings
from .discovery import DiscoveredCompany, normalize_domain
from .openai_discovery import public_url, discover_openai, DiscoveryError

logger = logging.getLogger(__name__)

# Approximate city centres are used ONLY for a bounded local OSM query.
CITIES = {
    "oosterhout": (51.644, 4.859), "breda": (51.588, 4.776),
    "tilburg": (51.555, 5.091), "eindhoven": (51.441, 5.479),
    "den bosch": (51.690, 5.303), "s-hertogenbosch": (51.690, 5.303),
    "roosendaal": (51.530, 4.465), "waalwijk": (51.684, 5.068),
    "helmond": (51.479, 5.661), "oss": (51.767, 5.523),
    "bergen op zoom": (51.495, 4.287), "etten-leur": (51.570, 4.637),
    "veghel": (51.616, 5.548), "veldhoven": (51.419, 5.403),
    "dongen": (51.627, 4.942), "rijen": (51.590, 4.918),
    "made": (51.677, 4.794), "amsterdam": (52.367, 4.904),
    "rotterdam": (51.924, 4.478), "den haag": (52.079, 4.313),
    "utrecht": (52.090, 5.121), "nijmegen": (51.843, 5.852),
    "arnhem": (51.985, 5.911), "apeldoorn": (52.211, 5.969),
    "zwolle": (52.516, 6.084), "maastricht": (50.851, 5.690),
    "haarlem": (52.388, 4.647), "dordrecht": (51.813, 4.690),
}
ENDPOINTS = ("https://overpass.private.coffee/api/interpreter",
             "https://overpass-api.de/api/interpreter")
BLOCKED_HOSTS = {
    "facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com",
    "tiktok.com", "google.com", "linktr.ee", "webwinkelkeur.nl",
    "tripadvisor.com", "booking.com", "funda.nl", "yelp.com",
    "openstreetmap.org", "marktplaats.nl",
}
_LOCK = threading.Lock()
_CACHE = {}
_LAST_REQUEST = 0.0
_BLOCKED_UNTIL = 0.0

def _segment(purpose: str, niche: str, query: str) -> str:
    s = f"{niche} {query}".lower()
    if any(k in s for k in ("sport", "fitness", "gym", "dans", "vecht", "yoga", "klim", "jump")):
        return "sport"
    if any(k in s for k in ("restaurant", "horeca", "cafe", "café", "lunch", "hotel", "bakker", "koffie")):
        return "horeca"
    if any(k in s for k in ("evenement", "festival", "club", "vereniging", "carnaval", "muziek", "theater", "lustrum")):
        return "community"
    if any(k in s for k in ("winkel", "speciaalzaak", "retail", "beauty", "kapper", "salon")):
        return "retail"
    if any(k in s for k in ("bouw", "installatie", "schilder", "industrie", "productie", "transport")):
        return "trade"
    return "services" if purpose == "design" else "trade"

# Query only a subset of places with an OSM website tag. Use bounded radius and
# capped output: no all-Netherlands OSM harvesting from the public service.
SELECTORS = {
    "sport": (('leisure', 'fitness_centre|sports_centre|dance|pitch|stadium'),
              ('sport', '.*')),
    "horeca": (('amenity', 'restaurant|cafe|bar|pub|fast_food'),
               ('tourism', 'hotel|hostel')),
    "community": (('amenity', 'community_centre|arts_centre|events_venue|theatre|social_centre'),
                  ('club', '.*')),
    "retail": (('shop', '.*'),),
    "trade": (('craft', '.*'), ('office', 'company|construction|logistics'),
              ('industrial', '.*')),
    "services": (('office', '.*'), ('amenity', 'clinic|doctors|dentist'),
                 ('shop', 'hairdresser|beauty|yes')),
}

def _query(lat: float, lon: float, segment: str) -> str:
    statements = []
    for key, pattern in SELECTORS[segment]:
        # All keys/patterns are internal constants, never user-supplied regex.
        statements.append(
            f'nwr(around:3000,{lat:.4f},{lon:.4f})["name"]["website"]["{key}"~"^{pattern}$"];'
        )
        statements.append(
            f'nwr(around:3000,{lat:.4f},{lon:.4f})["name"]["contact:website"]["{key}"~"^{pattern}$"];'
        )
    return "[out:json][timeout:18];(" + "".join(statements) + ");out tags center 120;"

def _valid_site(value: str) -> str:
    site = public_url(value)
    if not site:
        return ""
    domain = normalize_domain(site)
    if not domain or not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain):
        return ""
    if any(domain == h or domain.endswith("." + h) for h in BLOCKED_HOSTS):
        return ""
    return site

def _parse_elements(elements: list, limit: int) -> list[DiscoveredCompany]:
    found, seen = [], set()
    for element in elements:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") or {}
        if not isinstance(tags, dict):
            continue
        name = str(tags.get("name") or "").strip()
        site = _valid_site(tags.get("website") or tags.get("contact:website") or "")
        if not site or not name or len(name) > 220:
            continue
        domain = normalize_domain(site)
        if domain in seen:
            continue
        seen.add(domain)
        found.append(DiscoveredCompany(
            name=name, website=site, source="openstreetmap"
        ))
        if len(found) >= limit:
            break
    return found

def discover_open_data(query: str, region: str, limit: int = 12, *,
                       purpose: str = "merch", niche: str = "") -> list[DiscoveredCompany]:
    """One polite HTTP request for nearby published websites; no paid API."""
    global _LAST_REQUEST, _BLOCKED_UNTIL
    key = region.strip().lower()
    if key == "nederland":
        key = "breda"
    coords = CITIES.get(key)
    if not coords:
        logger.warning("No open-data city mapping for %s", key)
        return []
    segment = _segment(purpose, niche, query)
    cache_key = (key, segment)
    now = time.monotonic()
    with _LOCK:
        cached = _CACHE.get(cache_key)
        if cached and now - cached[0] < 6 * 3600:
            return _parse_elements(cached[1], max(1, min(20, limit)))
        if now < _BLOCKED_UNTIL:
            raise DiscoveryError("open_data_source_cooldown")
        # The HTTP request is performed under the process lock: never run
        # parallel Overpass requests from one worker.
        wait = max(0, 5 - (now - _LAST_REQUEST))
        if wait:
            time.sleep(wait)
        _LAST_REQUEST = time.monotonic()
        elements = None
        for endpoint in ENDPOINTS:
            try:
                with httpx.Client(timeout=25) as client:
                    resp = client.post(
                        endpoint, data={"data": _query(*coords, segment)},
                        headers={"User-Agent": "SuperMerch-Superfris-Leads/1.1 (+https://supermerch.nl; ODbL attribution)"},
                    )
                # Never evade a public host's rate limits with another host.
                if resp.status_code in (429, 406, 503, 504):
                    _BLOCKED_UNTIL = time.monotonic() + 600
                    logger.warning("OSM server busy: HTTP %s; cooldown 600s", resp.status_code)
                    raise DiscoveryError("open_data_source_rate_limited")
                resp.raise_for_status()
                elements = resp.json().get("elements", [])
                if not isinstance(elements, list):
                    elements = []
                break
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ProxyError) as exc:
                logger.warning("OSM endpoint unreachable (category=%s); alternate endpoint next",
                               type(exc).__name__)
                continue
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                _BLOCKED_UNTIL = time.monotonic() + 600
                logger.warning("OSM search unavailable (category=%s)", type(exc).__name__)
                raise DiscoveryError("open_data_source_failed") from None
        if elements is None:
            _BLOCKED_UNTIL = time.monotonic() + 600
            logger.warning("OSM all endpoints unreachable; no companies invented")
            raise DiscoveryError("open_data_source_unreachable")
        _CACHE[cache_key] = (time.monotonic(), elements)
        if len(_CACHE) > 100:
            _CACHE.clear()
    return _parse_elements(elements, max(1, min(20, limit)))

def discover_businesses(query: str, region: str, limit: int = 12, *,
                        purpose: str = "merch", niche: str = "") -> list[DiscoveredCompany]:
    """Select no-key discovery by default; optionally use paid web discovery."""
    if settings.free_mode:
        return discover_open_data(query, region, limit, purpose=purpose, niche=niche)
    return discover_openai(query, region, limit, purpose=purpose)
