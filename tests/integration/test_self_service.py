"""Phase 6 sub-step A against Neon: registration, quotas, admin user management, audit rows."""

from __future__ import annotations

import secrets
import uuid

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db.session import admin_transaction
from tests.integration import world
from tests.integration.conftest import PREFIX

pytestmark = pytest.mark.neon

PW = "Correct-Horse-" + "battery9"  # test-only value, not a real credential


def _slug() -> str:
    return PREFIX + secrets.token_hex(3)


def register(client, slug: str | None = None, email: str | None = None, **extra):  # type: ignore[no-untyped-def]
    slug = slug or _slug()
    body = {"company_name": f"Company {slug}", "slug": slug,
            "admin_email": email or f"admin@{slug}.example", "admin_password": PW, **extra}  # fmt: skip
    return client.post("/tenants/register", json=body)


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(client, email: str, password: str = PW) -> dict[str, str]:  # type: ignore[no-untyped-def]
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return _h(r.json()["access_token"])


def test_register_creates_tenant_and_first_admin(fresh, client):
    r = register(client)
    assert r.status_code == 201, r.text
    out = r.json()
    me = client.get("/auth/me", headers=_h(out["access_token"])).json()
    assert me == {"id": out["user_id"], "tenant_id": out["tenant_id"], "roles": ["admin"]}
    with admin_transaction() as s:
        src = s.execute(text("SELECT source FROM tenants WHERE id = :t"),
                        {"t": out["tenant_id"]}).scalar_one()  # fmt: skip
        ev = s.execute(
            text("SELECT event_type, target_type, target_id FROM retrieval_audit "
                 "WHERE tenant_id = :t ORDER BY ts"), {"t": out["tenant_id"]}
        ).all()  # fmt: skip
    assert src == "self_service"
    assert [tuple(e) for e in ev] == [("tenant_registered", "tenant", out["slug"])]


def test_register_rejects_client_chosen_identity_and_bad_input(fresh, client):
    for extra in ({"tenant_id": str(uuid.uuid4())}, {"roles": ["admin"]}, {"user_id": "x"}):
        assert register(client, **extra).status_code == 422
    assert register(client, slug="admin").status_code == 422  # too short / reserved
    assert register(client, slug="Bad Slug!").status_code == 422
    r = client.post("/tenants/register", json={"company_name": "X Co", "slug": _slug(),
                    "admin_email": "a@b.example", "admin_password": "short"})  # fmt: skip
    assert r.status_code == 422


def test_reserved_slug_rejected_by_database(fresh, client):
    r = register(client, slug="support")
    assert r.status_code == 422 and "reserved" in r.json()["detail"]


def test_duplicate_slug_or_email_same_generic_409(fresh, client):
    slug = _slug()
    assert register(client, slug=slug).status_code == 201
    dup_slug = register(client, slug=slug, email=f"other@{slug}.example")
    dup_email = register(client, email=f"admin@{slug}.example")
    seed_email = register(client, email=world.email("itest-a:employee_1"))
    assert dup_slug.status_code == dup_email.status_code == seed_email.status_code == 409
    assert dup_slug.json() == dup_email.json() == seed_email.json()


def test_registration_disabled_and_code(fresh, client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "registration_enabled", False)
    assert register(client).status_code == 403
    assert client.get("/tenants/registration-info").json()["enabled"] is False
    monkeypatch.setattr(s, "registration_enabled", True)
    monkeypatch.setattr(s, "registration_code", "invite-only-code")
    assert register(client).status_code == 403
    assert register(client, registration_code="invite-only-code").status_code == 201


def test_tenant_cap(fresh, client, monkeypatch):
    with admin_transaction() as s:
        n = s.execute(text("SELECT count(*) FROM tenants")).scalar_one()
    monkeypatch.setattr(get_settings(), "max_tenants", n)
    r = register(client)
    assert r.status_code == 409 and "limit" in r.json()["detail"]


def test_admin_creates_users_in_own_tenant_only(fresh, client):
    out = register(client).json()
    h = _h(out["access_token"])
    slug = out["slug"]
    r = client.post("/admin/users", headers=h, json={"email": f"hr@{slug}.example",
                    "roles": ["hr", "employee"], "password": PW})  # fmt: skip
    assert r.status_code == 201, r.text
    assert r.json()["roles"] == ["employee", "hr"]
    me = client.get("/auth/me", headers=_login(client, f"hr@{slug}.example")).json()
    assert me["tenant_id"] == out["tenant_id"] and me["roles"] == ["employee", "hr"]
    # the list is this tenant's users only, without password hashes
    users = client.get("/admin/users", headers=h)
    assert {u["email"] for u in users.json()} == {f"admin@{slug}.example", f"hr@{slug}.example"}
    assert "password" not in users.text and "argon2" not in users.text
    # a seed-tenant admin never sees the new tenant's users
    seed_admin = client.get("/admin/users", headers=_login(client, world.email("itest-a:admin_1"),
                                                           world.PASSWORD)).json()  # fmt: skip
    assert all(not u["email"].endswith(f"{slug}.example") for u in seed_admin)
    with admin_transaction() as s:
        ev = s.execute(text("SELECT event_type FROM retrieval_audit WHERE tenant_id = :t "
                            "ORDER BY ts"), {"t": out["tenant_id"]}).scalars().all()  # fmt: skip
    assert ev == ["tenant_registered", "user_created"]


def test_create_user_denials(fresh, client, monkeypatch):
    out = register(client).json()
    h, slug = _h(out["access_token"]), out["slug"]
    body = {"email": f"e@{slug}.example", "roles": ["employee"], "password": PW}
    assert client.post("/admin/users", json=body).status_code == 401
    emp = _login(client, world.email("itest-a:employee_1"), world.PASSWORD)
    assert client.post("/admin/users", headers=emp, json=body).status_code == 403
    for bad in ({**body, "roles": ["contractor"]}, {**body, "roles": ["superuser"]},
                {**body, "tenant_id": str(world.tid("itest-b"))}, {**body, "roles": []}):  # fmt: skip
        assert client.post("/admin/users", headers=h, json=bad).status_code == 422
    assert client.post("/admin/users", headers=h, json=body).status_code == 201
    assert client.post("/admin/users", headers=h, json=body).status_code == 409  # duplicate
    monkeypatch.setattr(get_settings(), "max_users_per_tenant", 2)
    r = client.post("/admin/users", headers=h, json={**body, "email": f"f@{slug}.example"})
    assert r.status_code == 409 and "quota" in r.json()["detail"]


def test_definer_functions_ignore_a_forged_tenant(fresh, client):
    """admin_create_user takes the tenant from the bound identity: a second tenant's admin who
    calls it lands the user in THEIR OWN tenant, never in another one."""
    a = register(client).json()
    b = register(client).json()
    hb = _h(b["access_token"])
    r = client.post("/admin/users", headers=hb, json={"email": f"x@{a['slug']}.example",
                    "roles": ["employee"], "password": PW})  # fmt: skip
    assert r.status_code == 201
    me = client.get("/auth/me", headers=_login(client, f"x@{a['slug']}.example")).json()
    assert me["tenant_id"] == b["tenant_id"] != a["tenant_id"]
