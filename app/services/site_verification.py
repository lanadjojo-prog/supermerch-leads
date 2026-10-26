"""Independent live verification for official prospect websites.

The web-search API does not consistently expose citations/source metadata.
Only a public, reachable HTML page on the claimed domain can be substituted.
No emails or organizational facts are inferred by this probe.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from .discovery import normalize_domain

logger = logging.getLogger(__name__)


def _public_dns_host(url: str) -> bool:
    host = urlsplit(url).hostname
    if not host:
        return False
    try:
        results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return bool(results) and all(
            ipaddress.ip_address(result[4][0]).is_global for result in results
        )
    except (OSError, ValueError):
        return False


def verify_live_business_url(candidate_url: str) -> str:
    """Return actually fetched HTML URL or empty string; never trust a model URL.

    Reject local/private DNS results and cross-domain redirects. Do not follow
    redirects blindly into a different hostname or fetch arbitrary large files.
    """
    from .openai_discovery import public_url

    url = public_url(candidate_url)
    if not url:
        return ""
    original_domain = normalize_domain(url)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SuperMerchLeadResearch/1.0)"}
    try:
        with httpx.Client(timeout=8.0, follow_redirects=False, headers=headers) as client:
            for _ in range(4):
                if not _public_dns_host(url):
                    return ""
                with client.stream("GET", url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        next_url = public_url(urljoin(url, response.headers.get("location", "")))
                        if not next_url or normalize_domain(next_url) != original_domain:
                            return ""
                        url = next_url
                        continue
                    if response.status_code != 200:
                        return ""
                    if "text/html" not in response.headers.get("content-type", "").lower():
                        return ""
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk[:max(0, 120000 - len(body))])
                        if len(body) >= 120000:
                            break
                    # A non-trivial response is needed; the full research crawler
                    # subsequently checks context and official contact evidence.
                    if len(body) < 120:
                        return ""
                    return url
    except (httpx.HTTPError, OSError, ValueError):
        logger.info("Candidate website not reachable or not public")
    return ""
