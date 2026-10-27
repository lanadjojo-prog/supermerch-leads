"""Accept only explicit Instagram profile links, never guessed handles."""
import re
from urllib.parse import urlparse

RESERVED = {"p", "reel", "reels", "stories", "explore", "accounts", "direct", "about", "legal", "developer", "share", "tv"}

def instagram_username(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or parsed.hostname not in {"instagram.com", "www.instagram.com"}:
        return None
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 1:
        return None
    name = parts[0].lower()
    if name in RESERVED or not re.fullmatch(r"[a-z0-9_][a-z0-9_.]{0,29}", name):
        return None
    return name
