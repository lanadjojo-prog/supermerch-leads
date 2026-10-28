"""Safe Instagram smoke tests run in the real Render Python environment.

All network calls are mocked and all database records are SQLite in-memory.
Run: python -m app.instagram_selftest
"""
from __future__ import annotations

import io
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from .database import Base
from .models import Campaign, ChatCommand, InstagramAction, InstagramProspect, Lead
from .services import crawler, instagram_engine as ig, instagram_priority as priority
from .services.instagram_links import instagram_username

REPO_SMOKE_MARKER = "mailing-priority-v2"
NOW = datetime.now(timezone.utc)


class InstagramSmoke(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.db.add(Campaign(id=1, name="Smoke fixture", niche="sport", region="NL",
                             search_query="sports clubs", target_count=1))
        self.db.add(Lead(id=1, campaign_id=1, company_name="Existing Club",
                         domain="club.example.org", website="https://club.example.org",
                         status="ready_for_review", score=80))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_limits_from_environment(self):
        self.assertEqual(ig.DAILY_ACCOUNTS_LIMIT, 50)
        self.assertEqual(ig.DAILY_LIMIT, 100)
        self.assertEqual(ig.PER_ACCOUNT, 2)

    def test_csv_import_is_private_and_deduplicated(self):
        payload = (
            "bedrijfsnaam;e-mail;website\n"
            "Club A;bestuur@teamvoorbeeld.nl;https://teamvoorbeeld.nl\n"
            "Club A;secretariaat@teamvoorbeeld.nl;teamvoorbeeld.nl\n"
            "Privé persoon;test@gmail.com;\n"
        ).encode()
        result = priority.import_mailing_csv(self.db, payload)
        self.assertEqual((result["toegevoegd"], result["overgeslagen"]), (1, 1))
        self.assertEqual(priority.import_mailing_csv(self.db, payload)["al_bekend"], 1)
        row = self.db.scalar(select(ChatCommand).where(ChatCommand.action == "instagram_priority"))
        self.assertEqual(row.status, "queued")
        self.assertNotIn("@", row.result)
        self.assertEqual(priority.priority_counts(self.db)["wacht"], 1)

    def test_priority_scan_precedes_regular_leads(self):
        priority.import_mailing_csv(self.db, b"bedrijf,website\nTeamvoorbeeld,https://teamvoorbeeld.nl\n")
        visited = []
        def fake_crawl(url):
            visited.append(url)
            return SimpleNamespace(
                pages=[(url, "Teamwebsite")],
                instagram_profiles=[("teamvoorbeeld", url + "/contact")],
            )
        with patch.object(priority, "crawl_website", side_effect=fake_crawl), \
             patch.object(ig, "crawl_website", side_effect=AssertionError("regular lead ran")), \
             patch.object(ig, "DAILY_ACCOUNTS_LIMIT", 1):
            first = ig.discover(self.db)
            self.assertEqual(first["accounts_found"], 1)
            self.assertEqual(first["priority_websites_scanned"], 1)
            self.assertEqual(ig.discover(self.db)["websites_scanned"], 0)
        self.assertEqual(visited, ["https://teamvoorbeeld.nl"])
        self.assertIsNotNone(self.db.scalar(select(InstagramProspect).where(
            InstagramProspect.username == "teamvoorbeeld")))

    def test_public_company_link_detection(self):
        self.assertEqual(instagram_username("https://instagram.com/TeamVoorbeeld/?a=1"), "teamvoorbeeld")
        self.assertIsNone(instagram_username("https://instagram.com/p/123"))
        original_client = httpx.Client
        def respond(request):
            if request.url.path == "/social-media":
                html = '<html><a href="https://instagram.com/teamvoorbeeld/">Volg ons</a></html>'
            else:
                html = '<html><a href="/social-media">Social</a></html>'
            return httpx.Response(200, headers={"content-type": "text/html"}, text=html)
        with patch.object(crawler, "_public_dns_host", return_value=True), \
             patch.object(crawler.httpx, "Client", side_effect=lambda **kw: original_client(
                 transport=httpx.MockTransport(respond), **kw)):
            found = crawler.crawl_website("https://club.example.org/")
        self.assertEqual(found.instagram_profiles[0][0], "teamvoorbeeld")

    def test_daily_reservations_prevent_duplicate_likes(self):
        def media(n):
            return {"id": str(n), "permalink": f"https://www.instagram.com/p/x{n}/"}
        with patch.object(ig, "DAILY_LIMIT", 3):
            for n in range(3):
                self.assertIsNotNone(ig.reserve_like(self.db, "testclub", media(n), "2026-10-28"))
            self.assertIsNone(ig.reserve_like(self.db, "testclub", media(3), "2026-10-28"))
            self.assertIsNone(ig.reserve_like(self.db, "testclub", media(0), "2026-10-29"))
        self.assertEqual(self.db.scalar(select(func.count(InstagramAction.id))), 3)

    def test_meta_calls_are_mocked_and_not_sent(self):
        seen = []
        def respond(request):
            seen.append((request.method, request.url.path))
            if request.method == "POST":
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json={"id": "123", "username": "supermerch.nl"})
        client = ig.MetaClient.__new__(ig.MetaClient)
        client.user = "123"
        client.client = httpx.Client(
            base_url="https://graph.facebook.com/v25.0/",
            transport=httpx.MockTransport(respond),
            headers={"Authorization": "Bearer fake-only-for-test"},
        )
        try:
            client.verify()
            client.like("456")
            self.assertEqual(seen, [("GET", "/v25.0/123"), ("POST", "/v25.0/123/likes")])
        finally:
            client.close()

    def test_large_inventory_keeps_imported_account_first(self):
        # A mailing-list profile at position >1000 must outrank old accounts.
        bulk = [InstagramProspect(username=f"oldclub{i}", lead_id=1,
                                  source_url="https://club.example.org")
                for i in range(1005)]
        bulk.append(InstagramProspect(username="newpriorityclub", lead_id=1,
                                     source_url="https://club.example.org"))
        self.db.add_all(bulk)
        self.db.commit()
        called = []
        class FakeClient:
            def verify(self): pass
            def media(self, username):
                called.append(username)
                return []
            def like(self, media_id):
                raise AssertionError("Smoke test must not issue likes")
        with patch.object(ig, "DAILY_ACCOUNTS_LIMIT", 1), \
             patch.object(priority, "priority_usernames", return_value={"newpriorityclub"}):
            result = ig.engage(self.db, FakeClient(), NOW)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(called, ["newpriorityclub"])

    def test_amsterdam_0900_in_winter_and_summer(self):
        with patch.dict("os.environ", {"INSTAGRAM_DAILY_NOT_BEFORE": "2026-10-29"}):
            self.assertFalse(ig._daily_due(datetime(2026, 10, 29, 7, 59, tzinfo=timezone.utc)))
            self.assertTrue(ig._daily_due(datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)))
            self.assertFalse(ig._daily_due(datetime(2026, 10, 28, 13, 0, tzinfo=timezone.utc)))
        with patch.dict("os.environ", {"INSTAGRAM_DAILY_NOT_BEFORE": ""}):
            self.assertTrue(ig._daily_due(datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc)))
            self.assertFalse(ig._daily_due(datetime(2026, 7, 1, 6, 59, tzinfo=timezone.utc)))

    def test_once_per_day_across_startup_and_health_wakes(self):
        discovered = []
        class FixedDate(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 29, 9, tzinfo=timezone.utc)
        with patch.object(ig, "datetime", FixedDate), \
             patch.object(ig, "SessionLocal", return_value=self.db), \
             patch.object(ig, "enabled", return_value=True), \
             patch.object(ig, "connection_ready", return_value=False), \
             patch.object(ig, "discover", side_effect=lambda db: (
                 discovered.append(True) or {"websites_scanned": 50, "accounts_found": 0}
             )), \
             patch.dict("os.environ", {"INSTAGRAM_DAILY_NOT_BEFORE": "2026-10-29"}):
            ig.run_background()
            ig.run_background()
        self.assertEqual(discovered, [True])
        runs = self.db.scalars(select(ChatCommand).where(
            ChatCommand.command_id == "instagram-day-2026-10-29")).all()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].status, "complete")

    def test_priority_queue_blocks_organic_likes(self):
        priority.import_mailing_csv(self.db, b"website\nhttps://club2.example.org\n")
        self.db.add(InstagramProspect(username="organicclub", lead_id=1,
                                      source_url="https://club.example.org"))
        self.db.commit()
        class ReadOnlyClient:
            def verify(self): pass
            def media(self, username):
                raise AssertionError("Must not engage organic account while list is queued")
        result = ig.engage(self.db, ReadOnlyClient(), NOW)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["likes"], 0)

    def test_csv_rejects_invalid_files(self):
        with self.assertRaises(ValueError):
            priority.parse_mailing_csv(b"onbekend,zonder-website\nA,B\n")
        with self.assertRaises(ValueError):
            priority.parse_mailing_csv(b"x" * 2_000_001)


def run_selftests():
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(InstagramSmoke)
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(suite)
    print(f"INSTAGRAM_SMOKETEST_{'PASS' if result.wasSuccessful() else 'FAIL'} "
          f"cases={result.testsRun} errors={len(result.errors)} failures={len(result.failures)} "
          f"marker={REPO_SMOKE_MARKER} external_likes=0 emails=0", flush=True)
    return result.wasSuccessful()


if __name__ == "__main__":
    sys.exit(0 if run_selftests() else 1)
