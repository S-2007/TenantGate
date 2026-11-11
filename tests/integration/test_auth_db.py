"""Auth end-to-end against Neon: real users table, real app role, real /ask (fake LLM, hash embedder)."""

from __future__ import annotations

import pytest
from sqlalchemy import update

from app.db.models import User
from app.db.session import admin_transaction
from tests.integration import world

pytestmark = pytest.mark.neon


def _login(client, key: str, password: str = world.PASSWORD):  # type: ignore[no-untyped-def]
    return client.post("/auth/login", json={"email": world.email(key), "password": password})


def _bearer(client, key: str) -> dict[str, str]:  # type: ignore[no-untyped-def]
    r = _login(client, key)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_login_success_and_me(seeded, client):
    h = _bearer(client, "itest-a:multi_1")
    me = client.get("/auth/me", headers=h).json()
    assert me == {
        "id": str(world.uid("itest-a:multi_1")),
        "tenant_id": str(world.tid("itest-a")),
        "roles": ["finance", "manager"],
    }


def test_login_failures_same_401(seeded, client):
    bad = _login(client, "itest-a:employee_1", "wrong-password")
    unknown = client.post("/auth/login", json={"email": "ghost@itest-a.example", "password": "x"})
    inactive = _login(client, "itest-a:off_1")
    assert bad.status_code == unknown.status_code == inactive.status_code == 401
    assert bad.json() == unknown.json() == inactive.json()


def test_roles_changed_in_db_since_issuance(seeded, client):
    h = _bearer(client, "itest-a:hr_1")
    with admin_transaction() as s:
        s.execute(
            update(User).where(User.id == world.uid("itest-a:hr_1")).values(roles=["employee"])
        )
    assert client.get("/auth/me", headers=h).json()["roles"] == ["employee"]


def test_deactivated_after_login_loses_access_on_next_request(seeded, client):
    h = _bearer(client, "itest-a:employee_1")
    assert client.get("/auth/me", headers=h).status_code == 200
    with admin_transaction() as s:
        s.execute(
            update(User).where(User.id == world.uid("itest-a:employee_1")).values(is_active=False)
        )
    assert client.get("/auth/me", headers=h).status_code == 401


def test_ask_requires_token_and_works_with_one(ingested, client):
    q = {"question": "How many sick days do Itest A staff receive per year?"}
    assert client.post("/ask", json=q).status_code == 401
    r = client.post("/ask", json=q, headers=_bearer(client, "itest-a:employee_1"))
    assert r.status_code == 200, r.text
    assert r.json()["retrieved_chunk_ids"]


def test_ask_rejects_body_tenant_id(seeded, client):
    h = _bearer(client, "itest-a:employee_1")
    r = client.post(
        "/ask", json={"question": "hi", "tenant_id": str(world.tid("itest-b"))}, headers=h
    )
    assert r.status_code == 422
