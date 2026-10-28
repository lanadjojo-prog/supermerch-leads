"""Offline regression tests for purchase-intent gating; no network or email."""
import unittest
from datetime import date

from app.services.purchase_intent import detect_purchase_opportunities
from app.services.scoring import score_lead


TODAY = date(2026, 10, 24)


class PurchaseIntentTests(unittest.TestCase):
    def detect(self, text):
        return detect_purchase_opportunities(
            [("https://club.example.nl/nieuws", text)], today=TODAY
        )

    def test_undated_generic_merch_text_is_not_purchase_intent(self):
        self.assertEqual(self.detect("Wij organiseren toernooien en verkopen merchandise."), [])

    def test_stale_announcement_is_not_current(self):
        self.assertEqual(self.detect("Ons jubileum vindt plaats op 12 juni 2024."), [])

    def test_future_event_with_full_date_is_an_opportunity_not_a_buying_request(self):
        found = self.detect("Op 12 november 2026 organiseren we een sporttoernooi.")
        self.assertTrue(found)
        self.assertEqual(found[0]["trigger_type"], "intent_upcoming_event")
        self.assertEqual(found[0]["event_date"], "2026-11-12")
        self.assertTrue(found[0]["source_url"].startswith("https://"))

    def test_explicit_dated_request_gets_strong_intent(self):
        found = self.detect(
            "20 oktober 2026: wij zoeken een leverancier voor 200 bedrukte t-shirts."
        )
        self.assertTrue(found)
        self.assertEqual(found[0]["trigger_type"], "intent_active_request")

    def test_far_future_event_is_not_an_immediate_opportunity(self):
        self.assertEqual(self.detect("Festival op 1 juli 2028."), [])

    def test_dated_carnival_calendar_is_an_event_not_a_purchase_request(self):
        found = self.detect("Evenementen Elluf Elluf 2026 Datum: 11/11/2026 Tijd: 13:11")
        self.assertTrue(found)
        self.assertEqual(found[0]["trigger_type"], "intent_upcoming_event")

    def test_unrelated_supplier_request_is_not_merch_intent(self):
        self.assertEqual(
            self.detect("20 oktober 2026: wij zoeken een leverancier voor koffiebonen."),
            [],
        )

    def test_unverified_fit_cannot_enter_review(self):
        score = score_lead(
            {"merch_signal": "yes", "event_signal": "yes"},
            "Sportclubs",
            triggers=[{"trigger_type": "event", "strength": 34}],
            contact_available=True,
        )
        self.assertFalse(score.auto_eligible)
        self.assertLess(score.score, 75)

    def test_verified_opportunity_can_enter_human_review(self):
        opportunities = self.detect("Op 12 november 2026 organiseren we een sporttoernooi.")
        scored = score_lead({}, "Sportclubs", opportunities=opportunities,
                            contact_available=True)
        self.assertTrue(scored.auto_eligible)
        self.assertGreaterEqual(scored.score, 75)

    def test_missing_contact_blocks_auto_drafting(self):
        opportunities = self.detect("Op 12 november 2026 organiseren we een sporttoernooi.")
        scored = score_lead({}, "Sportclubs", opportunities=opportunities,
                            contact_available=False)
        self.assertFalse(scored.auto_eligible)


if __name__ == "__main__":
    unittest.main()
