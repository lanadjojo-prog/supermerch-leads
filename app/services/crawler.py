from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..config import settings

EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
RELEVANT_HINTS = (
    "contact", "over-ons", "about", "team", "vacature", "vacatures",
    "werken-bij", "careers", "jobs", "nieuws", "news", "event", "events",
    "agenda", "beurs", "expo", "congres", "festival", "toernooi",
    "jubileum", "lustrum", "opening", "heropening", "vestiging", "locaties",
    "rebrand", "huisstijl", "nieuw-logo", "sponsor", "partners", "pers", "blog",
)


@dataclass
class CrawlResult:
    pages: list[tuple[str, str]] = field(default_factory=list)
    emails: list[tuple[str, str]] = field(default_factory=list)

    @property
    def combined_text(self) -> str:
        return "\n\n".join(f"URL: {url}\n{text}" for url, text in self.pages)


def _clean_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    return re.sub(r"\s+", " ", " ".join(soup.stripped_strings)).strip()


def _host(url: str) -> str:
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def crawl_website(start_url: str) -> CrawlResult:
    result = CrawlResult()
    if not start_url.startswith(("http://", "https://")):
        start_url = "https://" + start_url

    candidates = [start_url]
    visited: set[str] = set()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SuperMerchLeadResearch/1.0; +https://supermerch.nl/)"}

    with httpx.Client(timeout=settings.crawl_timeout_seconds, follow_redirects=True, headers=headers) as client:
        while candidates and len(result.pages) < settings.max_crawl_pages:
            url = candidates.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                response = client.get(url)
                if response.status_code >= 400 or "text/html" not in response.headers.get("content-type", ""):
                    continue
            except httpx.HTTPError:
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            text = _clean_text(soup)
            if text:
                result.pages.append((str(response.url), text[:18000]))

            for email in sorted(set(EMAIL_RE.findall(soup.get_text(" ", strip=True)))):
                lower = email.lower()
                if any(x in lower for x in ("example.com", "sentry.io", "wixpress.com")):
                    continue
                pair = (lower, str(response.url))
                if pair not in result.emails:
                    result.emails.append(pair)

            for a in soup.find_all("a", href=True):
                href = urljoin(str(response.url), a["href"].split("#")[0])
                if _host(start_url) != _host(href):
                    continue
                path = urlparse(href).path.lower().strip("/")
                anchor = a.get_text(" ", strip=True).lower()
                if any(h in path or h in anchor for h in RELEVANT_HINTS):
                    if href not in visited and href not in candidates:
                        candidates.append(href)
    return result
