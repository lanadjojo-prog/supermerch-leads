import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from app.services.openai_discovery import (
    DiscoveryError, discover_openai, parse_companies, public_url,
)


def response(entries, sources=(), searched=True):
    output = [{"type": "message", "content": [{"type": "output_text",
               "text": json.dumps({"companies": entries}), "annotations": []}]}]
    if searched:
        output.insert(0, {"type": "web_search_call", "status": "completed",
                         "action": {"sources": [{"url": u} for u in sources]}})
    return {"status": "completed", "output": output}


class DiscoveryTests(unittest.TestCase):
    def test_only_sourced_companies_and_unique_domains(self):
        urls = ["https://club.nl/feest", "https://www.club.nl/contact"]
        data = response([
            {"name": "Club", "website": urls[0]},
            {"name": "Duplicate", "website": urls[1]},
            {"name": "Invented", "website": "https://invented.nl"},
        ], urls)
        found = parse_companies(data, 12)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].website, urls[0])
        self.assertEqual(found[0].source, "openai_web_search")

    def test_search_is_mandatory(self):
        with self.assertRaisesRegex(DiscoveryError, "search_not_executed"):
            parse_companies(response([], searched=False), 12)

    def test_no_sources_is_failure_not_success(self):
        with self.assertRaisesRegex(DiscoveryError, "no_verified_sources"):
            parse_companies(response([{"name": "Club", "website": "https://club.nl"}]), 12)

    def test_incomplete_and_bad_json_are_errors(self):
        data = response([])
        data["status"] = "incomplete"
        with self.assertRaises(DiscoveryError):
            parse_companies(data, 12)
        data = response([])
        data["output"][-1]["content"][0]["text"] = "Not JSON"
        with self.assertRaisesRegex(DiscoveryError, "invalid_json"):
            parse_companies(data, 12)

    def test_citation_sources_supported(self):
        url = "https://club.nl/agenda"
        data = response([{"name": "Club", "website": url}])
        data["output"][-1]["content"][0]["annotations"] = [{"type": "url_citation", "url": url}]
        self.assertEqual(len(parse_companies(data, 1)), 1)

    def test_reject_unsafe_urls(self):
        for value in [None, "file:///etc/passwd", "http://127.0.0.1", "http://169.254.169.254/", "http://localhost", "https://user:pass@club.nl", "https://club.nl:9999"]:
            self.assertEqual(public_url(value), "")

    def test_api_requests_live_search_and_bounded_output(self):
        with patch("app.services.openai_discovery.settings", SimpleNamespace(
            openai_api_key="test", openai_discovery_model="gpt-4.1",
            local_timezone=__import__('datetime').timezone.utc,
        )), patch("app.services.openai_discovery.OpenAI") as factory:
            client = factory.return_value.__enter__.return_value
            client.responses.create.return_value.model_dump.return_value = response([])
            self.assertEqual(discover_openai("club", "Breda", 5), [])
            args = client.responses.create.call_args.kwargs
            self.assertEqual(args["tool_choice"], "required")
            self.assertEqual(args["tools"], [{"type": "web_search"}])
            self.assertIn("web_search_call.action.sources", args["include"])


if __name__ == "__main__":
    unittest.main()
