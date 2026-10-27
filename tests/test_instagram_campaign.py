from datetime import datetime, timedelta, timezone

from app.services.instagram_campaign import (
    _is_suitable, username_from_company_homepage, username_from_url,
)


def test_accepts_public_company_profiles_only():
    assert username_from_url("https://www.instagram.com/brand_name/") == "brand_name"
    assert username_from_url("https://instagram.com/p/abc123/") is None
    assert username_from_url("https://evil.example/redirect?to=instagram.com") is None
    assert username_from_url("https://instagram.com/stories/foo") is None
    assert username_from_url("https://instagram.com/brand_name/other") is None


def test_extracts_official_link_from_company_website():
    html = '<a href="https://www.instagram.com/examplebrand/">Volg ons</a>'
    assert username_from_company_homepage(html) == "examplebrand"


def test_rejects_stale_and_sensitive_media():
    now = datetime.now(timezone.utc)
    media = {
        "id": "123", "permalink": "https://instagram.com/p/abc/",
        "media_type": "IMAGE", "caption": "Ons nieuwe team!",
        "timestamp": (now - timedelta(days=2)).isoformat(),
    }
    assert _is_suitable(media, now)
    assert not _is_suitable({**media, "caption": "Wij zijn in rouw"}, now)
    assert not _is_suitable({**media, "timestamp": (now-timedelta(days=120)).isoformat()}, now)
