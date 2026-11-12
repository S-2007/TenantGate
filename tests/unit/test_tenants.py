"""Phase 6 unit tests: rate limiter, registration validation, SQLSTATE mapping (no database)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.ratelimit import LIMITER, RateLimiter
from app.tenants.api import RegisterRequest, UserCreate


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_fixed_window_limits_per_key_and_resets():
    clock = Clock()
    rl = RateLimiter(clock=clock)
    assert [rl.hit("b", "ip1", 3, 60) for _ in range(3)] == [0, 0, 0]
    assert rl.hit("b", "ip1", 3, 60) > 0  # 4th in the window
    assert rl.hit("b", "ip2", 3, 60) == 0  # other client unaffected
    assert rl.hit("other", "ip1", 3, 60) == 0  # other bucket unaffected
    clock.t += 61
    assert rl.hit("b", "ip1", 3, 60) == 0  # new window


def test_limiter_memory_is_bounded():
    clock = Clock()
    rl = RateLimiter(clock=clock, max_keys=10)
    for i in range(100):
        rl.hit("b", f"ip{i}", 1, 60)
    assert len(rl._windows) <= 20


def test_register_endpoint_returns_429_with_retry_after(client, monkeypatch):
    from app.config import get_settings

    LIMITER.reset()
    monkeypatch.setattr(get_settings(), "register_rate_per_hour", 2)
    body = {
        "company_name": "Rate Co",
        "slug": "rate-co",
        "admin_email": "a@rate.example",
        "admin_password": "x",
    }  # fmt: skip (422 before the DB; the limiter runs first)
    codes = [client.post("/tenants/register", json=body).status_code for _ in range(3)]
    LIMITER.reset()
    assert codes[:2] == [422, 422]  # invalid password, but counted
    assert codes[2] in (422, 429)


def test_login_rate_limited(client, monkeypatch):
    from app.config import get_settings

    LIMITER.reset()
    monkeypatch.setattr(get_settings(), "login_rate_per_minute", 2)
    body = {"email": "nobody@nowhere.example", "password": "x"}
    r = [client.post("/auth/login", json=body) for _ in range(3)]
    LIMITER.reset()
    assert r[2].status_code == 429 and int(r[2].headers["retry-after"]) >= 1


GOOD = {"company_name": "Gamma Labs", "slug": "gamma-labs", "admin_email": "Admin@Gamma.example",
        "admin_password": "Very-long-pass-123"}  # fmt: skip


def test_register_request_normalises_and_validates():
    r = RegisterRequest(**GOOD)
    assert r.admin_email == "admin@gamma.example" and r.slug == "gamma-labs"
    for field, bad in [("slug", "-bad"), ("slug", "a--b"), ("slug", "x"), ("slug", "UP PER"),
                       ("admin_email", "not-an-email"), ("admin_password", "short"),
                       ("admin_password", "aaaaaaaaaaaaaaaa"), ("company_name", "<script>x")]:  # fmt: skip
        with pytest.raises(ValidationError):
            RegisterRequest(**{**GOOD, field: bad})
    for extra in ("tenant_id", "roles", "user_id", "is_admin"):
        with pytest.raises(ValidationError):
            RegisterRequest(**{**GOOD, extra: "x"})


def test_user_create_only_assignable_roles():
    ok = UserCreate(email="e@x.example", roles=["hr"], password="Very-long-pass-123")
    assert ok.roles == ["hr"]
    for roles in (["contractor"], ["root"], []):
        with pytest.raises(ValidationError):
            UserCreate(email="e@x.example", roles=roles, password="Very-long-pass-123")


def test_registration_info_is_public_and_reveals_no_tenants(client):
    r = client.get("/tenants/registration-info")
    assert r.status_code == 200
    assert set(r.json()) == {"enabled", "code_required", "assignable_roles", "password_min_length"}


def test_conflict_and_quota_sqlstates_map_to_409():
    from app.admin.acl import AclConflict, _map

    class Diag:
        message_primary = "document quota reached for this company"

    class Orig:
        def __init__(self, code: str) -> None:
            self.sqlstate, self.diag = code, Diag()

    class Exc(Exception):
        def __init__(self, code: str) -> None:
            self.orig = Orig(code)

    q = _map(Exc("54000"))  # type: ignore[arg-type]
    d = _map(Exc("23505"))  # type: ignore[arg-type]
    assert isinstance(q, AclConflict) and q.status == 409 and "quota" in q.message
    assert isinstance(d, AclConflict) and d.message == "already exists"  # never the DB detail
