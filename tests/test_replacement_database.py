"""A replacement database must boot without replaying mail history."""
from contextlib import contextmanager

import psycopg
import pytest

from app import safe_workspace2_entrypoint as boot


FLAGS = ("AUTOMATION_ENABLED", "AUTOMATED_OUTREACH_ENABLED",
         "INSTAGRAM_LIKES_ENABLED", "SUPERFRIS_ENABLED", "ZOHO_SELF_TEST",
         "ZOHO_DRAFT_SELF_TEST", "SUPERFRIS_DRAFT_SELFTEST_ON_STARTUP")


@pytest.fixture
def replacement(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/replacement?sslmode=require")
    monkeypatch.setenv("DATABASE_BOOTSTRAP_MODE", "fresh_discovery")
    for name in FLAGS:
        monkeypatch.setenv(name, "false")
    queries = []

    class Cursor:
        def execute(self, query):
            queries.append(query)

        def fetchone(self):
            return (1,) if queries[-1] == "SELECT 1" else (None, None, None, None)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Connection:
        def cursor(self):
            return Cursor()

    @contextmanager
    def connect(*args, **kwargs):
        yield Connection()

    monkeypatch.setattr(psycopg, "connect", connect)
    return queries


def test_empty_replacement_is_reachable_without_old_mailbox(replacement):
    assert boot._existing_database_preflight() == ""
    assert replacement == ["SET LOCAL statement_timeout = '6s'", "SELECT 1"]


@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("value", ["true", ""])
def test_replacement_refuses_automatic_mail_or_likes(replacement, monkeypatch, flag, value):
    monkeypatch.setenv(flag, value)
    assert flag in boot._existing_database_preflight()
    assert replacement == []


def test_default_still_requires_original_data(replacement, monkeypatch):
    monkeypatch.delenv("DATABASE_BOOTSTRAP_MODE")
    assert "existing lead tables missing" in boot._existing_database_preflight()


def test_replacement_still_requires_tls(replacement, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/replacement?sslmode=disable")
    assert "sslmode=require" in boot._existing_database_preflight()
    assert replacement == []


def test_failed_connection_never_echoes_credentials(replacement, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise psycopg.OperationalError("password=secret-do-not-print SSL failed")
    monkeypatch.setattr(psycopg, "connect", fail)
    assert "connection unavailable" in boot._existing_database_preflight()
    assert "secret-do-not-print" not in capsys.readouterr().out


def test_neon_uses_ipv4_without_replacing_tls_hostname(monkeypatch):
    import os
    import socket
    from urllib.parse import urlsplit, parse_qs
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@ep-example.neon.tech/replacement?sslmode=verify-full&channel_binding=require")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 5432))])
    boot._require_postgres_tls()
    parsed = urlsplit(os.environ["DATABASE_URL"])
    assert parsed.hostname == "ep-example.neon.tech"
    assert parse_qs(parsed.query) == {"sslmode": ["verify-full"],
        "channel_binding": ["require"], "hostaddr": ["192.0.2.1"]}


@pytest.mark.parametrize("action", ["fill_both_draft_queues", "run_daily_drafts",
    "save_curated_zoho_drafts", "send_direct_emails", "approve_review", "send_approved"])
def test_fresh_database_never_replays_mail_commands(monkeypatch, action):
    from app import command_bridge
    monkeypatch.setattr(command_bridge, "_load_command", lambda: {
        "command_id": "old-work", "action": action})
    def forbidden():
        raise AssertionError("Disallowed startup command must not open the database")
    monkeypatch.setattr(command_bridge, "SessionLocal", forbidden)
    command_bridge.process_startup_command(discovery_only=True)


def test_fresh_campaign_discovery_can_run_without_mailbox(monkeypatch):
    from app import command_bridge
    from app.database import Base
    from app.models import Campaign, ChatCommand
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    sessions = sessionmaker(bind=create_engine("sqlite://"))
    Base.metadata.create_all(sessions.kw["bind"])
    monkeypatch.setattr(command_bridge, "SessionLocal", sessions)
    monkeypatch.setattr(command_bridge, "_load_command", lambda: {
        "command_id": "new-discovery", "action": "start_campaign",
        "payload": {"niche": "Sportclubs", "region": "Nederland", "target_count": 3}})
    calls = []
    def discover(db, campaign):
        calls.append(campaign.target_count)
        campaign.status = "complete"
        db.commit()
        return {"created": 0}
    monkeypatch.setattr(command_bridge, "run_campaign", discover)
    command_bridge.process_startup_command(discovery_only=True)
    command_bridge.process_startup_command(discovery_only=True)
    assert calls == [3]
    with sessions() as db:
        assert db.scalar(select(Campaign)).status == "complete"
        assert db.scalar(select(ChatCommand)).status == "complete"
