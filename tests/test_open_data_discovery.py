"""Offline regression tests for no-key dual-brand discovery and safe website fetching."""
import unittest
from unittest.mock import Mock, patch

from app.services import open_data_discovery as data
from app.services.crawler import _safe_get


class FreeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        data._CACHE.clear()
        data._BLOCKED_UNTIL = 0
        data._LAST_REQUEST = 0

    def test_no_openai_or_google_key_required(self):
        response = Mock(status_code=200)
        response.json.return_value = {"elements": [
            {"tags": {"name": "Fietsclub", "website": "https://fietsclub-example.nl"}},
            {"tags": {"name": "Duplicate", "website": "https://fietsclub-example.nl/about"}},
            {"tags": {"name": "Social", "website": "https://facebook.com/example"}},
            {"tags": {"name": "Private", "website": "http://localhost/admin"}},
        ]}
        client = Mock()
        client.post.return_value = response
        manager = Mock()
        manager.__enter__ = Mock(return_value=client)
        manager.__exit__ = Mock(return_value=None)
        with patch("app.services.open_data_discovery.httpx.Client", return_value=manager):
            found = data.discover_open_data("sportschool", "Breda", 10, niche="Sport & fitness")
        self.assertEqual([(x.name, x.source) for x in found],
                         [("Fietsclub", "openstreetmap")])
        self.assertEqual(client.post.call_count, 1)
        self.assertIn("around:3000", client.post.call_args.kwargs["data"]["data"])
        self.assertIn("out tags center 120", client.post.call_args.kwargs["data"]["data"])

    def test_unknown_city_returns_empty_without_external_requests(self):
        with patch("app.services.open_data_discovery.httpx.Client") as client:
            self.assertEqual(data.discover_open_data("winkel", "Onbekend"), [])
            client.assert_not_called()

    def test_discovery_prefers_no_key_mode(self):
        with patch("app.services.open_data_discovery.settings") as settings:
            settings.free_mode = True
            with patch("app.services.open_data_discovery.discover_open_data", return_value=[]) as free:
                with patch("app.services.open_data_discovery.discover_openai") as ai:
                    self.assertEqual(data.discover_businesses("restaurant", "Breda", purpose="design"), [])
                    free.assert_called_once()
                    ai.assert_not_called()

    def test_safe_get_rejects_cross_host_redirect(self):
        first = Mock(status_code=302, headers={"location": "http://127.0.0.1/admin"})
        first.url = "https://example.nl"
        client = Mock()
        client.get.return_value = first
        with patch("app.services.crawler._public_dns_host", return_value=True):
            self.assertIsNone(_safe_get(client, "https://example.nl", "example.nl"))
        self.assertEqual(client.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
