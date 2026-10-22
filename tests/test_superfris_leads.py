"""Offline tests: no network, no real IMAP or outbound messages."""
import os
import unittest
from unittest.mock import MagicMock, patch

from app.superfris_leads import _compose_message, _save_strato_draft, _verified_company_email


class SuperfrisPipelineTests(unittest.TestCase):
    def test_only_accepts_email_from_business_website(self):
        self.assertEqual(
            _verified_company_email("example.nl", [
                ("info@example.nl", "https://example.nl/contact"),
                ("outside@other.nl", "https://listing.nl/contact"),
            ]),
            ("info@example.nl", "https://example.nl/contact")
        )
        self.assertIsNone(_verified_company_email(
            "example.nl", [("info@example.nl", "https://directory.nl/listing")]
        ))

    def test_conservative_copy_avoids_unverified_website_claims(self):
        with patch("app.superfris_leads.settings") as settings:
            settings.openai_api_key = ""
            text = _compose_message("Voorbeeld BV", "Breda", "Horeca",
                                    "grafisch ontwerp", "onbetrouwbare content")
        self.assertIn("Voorbeeld BV", text)
        self.assertIn("Giovanni van Superfris", text)
        self.assertNotIn("verouderde website", text)

    def test_appends_draft_without_sending(self):
        fake = MagicMock()
        fake.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "Drafts"'])
        fake.append.return_value = ("OK", [b"1"])
        with patch.dict(os.environ, {"SUPERFRIS_STRATO_PASSWORD": "fake-test-only"}):
            with patch("app.superfris_leads.imaplib.IMAP4_SSL", return_value=fake):
                _save_strato_draft("info@example.nl", "Onderwerp", "Hallo")
        fake.login.assert_called_once_with("gio@superfris.online", "fake-test-only")
        self.assertEqual(fake.append.call_count, 1)
        args = fake.append.call_args.args
        self.assertEqual(args[0], "Drafts")
        self.assertEqual(args[1], r"(\\Draft)")
        self.assertIn(b"info@example.nl", args[3])
        fake.logout.assert_called_once()


if __name__ == "__main__":
    unittest.main()
