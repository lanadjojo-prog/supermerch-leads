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
    assert replacement == ["SELECT 1"]


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
