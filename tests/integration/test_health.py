"""Smoke test: /health against the real Neon test branch."""

import pytest

pytestmark = pytest.mark.neon


def test_health_reports_db_and_vector(app_role, client):
    r = client.get("/health")
    body = r.json()
    assert r.status_code == 200, body
    assert body["status"] == "healthy"
    db = body["database"]
    assert db["ok"] is True
    assert db["vector_extension"]["installed"] is True
    assert db["chunks_table"]["dim_matches_config"] is True
    assert db["latency_ms"] >= 0
    text = r.text.lower()
    assert "password" not in text and "postgresql://" not in text
