"""Offline tests: no network, no real IMAP or outbound messages."""
import os
import unittest
from unittest.mock import MagicMock, patch

from app.superfris_leads import _compose_message, _save_strato_draft, _verified_company_email, _format_draft, _signature_html


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

    def test_html_alternative_uses_signature_and_public_logo(self):
        msg = _format_draft(
            "info@example.nl", "Een idee voor Example", 
            "Hallo,\n\nWe werken graag samen.\n\nGroet,\nGiovanni\nSuperfris",
            lead_id=123,
        )
        self.assertEqual(msg["X-Superfris-Lead-ID"], "123")
        self.assertTrue(msg.is_multipart())
        text = msg.get_body(preferencelist=("plain",)).get_content()
        html_part = msg.get_body(preferencelist=("html",)).get_content()
        self.assertIn("Met vriendelijke groet,", text)
        self.assertIn("Giovanni Djojowardi", text)
        self.assertIn("Gio@superfris.online", html_part)
        self.assertIn("+31 (0)6 14 99 01 12", html_part)
        self.assertIn("superfris-v7-00.png", html_part)
        self.assertNotIn("webmail.strato.com", html_part)
        self.assertNotIn("Groet,<br>Giovanni<br>Superfris", html_part)

    def test_html_escapes_company_text(self):
        msg = _format_draft("info@example.nl", "Onderwerp", "Hallo <script>alert(1)</script>")
        body = msg.get_body(preferencelist=("html",)).get_content()
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>", body)

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
        self.assertEqual(args[1], r"(\Draft)")
        self.assertIn(b"info@example.nl", args[3])
        fake.logout.assert_called_once()


if __name__ == "__main__":
    unittest.main()
