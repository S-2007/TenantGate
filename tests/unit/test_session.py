"""Connect retry with backoff (no network: psycopg.connect is stubbed)."""

import psycopg
import pytest

from app.db import session as sess


def test_retry_then_success(monkeypatch):
    calls = {"n": 0}

    def fake_connect(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise psycopg.OperationalError("compute waking up")
        return object()

    monkeypatch.setattr(sess.psycopg, "connect", fake_connect)
    monkeypatch.setattr(sess.time, "sleep", lambda s: None)
    stats = sess.ConnectStats()
    sess.connect_with_retry("postgresql://u:pw@h.neon.tech/db", stats=stats, backoff_s=0)
    assert calls["n"] == 3 and stats.retries == 2 and stats.first_connect_ms is not None


def test_gives_up_without_leaking_dsn(monkeypatch):
    def boom(*a, **k):
        raise psycopg.OperationalError("nope")

    monkeypatch.setattr(sess.psycopg, "connect", boom)
    monkeypatch.setattr(sess.time, "sleep", lambda s: None)
    with pytest.raises(ConnectionError) as ei:
        sess.connect_with_retry("postgresql://u:topsecretpw@h.neon.tech/db", attempts=2, stats=None)
    assert "topsecretpw" not in str(ei.value) and "h.neon.tech" in str(ei.value)
