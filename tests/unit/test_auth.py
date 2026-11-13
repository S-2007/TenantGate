"""Authentication: tokens, get_current_user and /auth/login, with an in-memory user store."""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid

import jwt
import pytest

from app.auth.dependencies import get_user_repository
from app.auth.passwords import verify_password
from app.auth.tokens import TokenError, decode_access_token, issue_access_token
from app.config import get_settings
from tests.unit.authkit import PASSWORD, T2, MemoryRepo, bearer, make_user, token_for


@pytest.fixture()
def repo(client):
    r = MemoryRepo()
    client.app.dependency_overrides[get_user_repository] = lambda: r
    yield r
    client.app.dependency_overrides.clear()


def _b64(obj) -> str:  # type: ignore[no-untyped-def]
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _claims(user, **over):  # type: ignore[no-untyped-def]
    s = get_settings()
    now = int(time.time())
    return {
        "sub": str(user.id),
        "tenant_id": str(user.tenant_id),
        "roles": list(user.roles),
        "tv": user.token_version,
        "iat": now,
        "exp": now + 600,
        "jti": str(uuid.uuid4()),
        "iss": s.jwt_issuer,
        "aud": s.jwt_audience,
    } | over


# ---------------- token encode / decode ----------------
def test_issued_token_has_all_claims_and_short_expiry():
    user = make_user()
    tok, ttl = issue_access_token(
        user_id=user.id, tenant_id=user.tenant_id, roles=user.roles, settings=get_settings()
    )
    c = decode_access_token(tok, settings=get_settings())
    assert (c.sub, c.tenant_id, c.roles) == (user.id, user.tenant_id, ("employee",))
    assert ttl == 15 * 60 and c.exp - c.iat == ttl and uuid.UUID(c.jti)
    assert jwt.get_unverified_header(tok)["alg"] == "HS256"


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda c: c | {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 4000}, "expired"),
        (lambda c: c | {"aud": "someone-else"}, "wrong_audience"),
        (lambda c: c | {"iss": "evil"}, "wrong_issuer"),
        (lambda c: {k: v for k, v in c.items() if k != "jti"}, "missing_claim"),
        (lambda c: {k: v for k, v in c.items() if k != "tenant_id"}, "missing_claim"),
        (lambda c: c | {"iat": int(time.time()) + 3600}, "not_yet_valid"),
        (lambda c: c | {"sub": "not-a-uuid"}, "invalid_claims"),
        (lambda c: c | {"roles": "admin"}, "invalid_claims"),
    ],
)
def test_bad_claims_rejected(mutate, reason):
    s = get_settings()
    tok = jwt.encode(mutate(_claims(make_user())), s.jwt_secret, algorithm="HS256")
    with pytest.raises(TokenError) as ei:
        decode_access_token(tok, settings=s)
    assert ei.value.reason == reason


def test_tampered_payload_fails_signature():
    s = get_settings()
    tok = jwt.encode(_claims(make_user()), s.jwt_secret, algorithm="HS256")
    head, _, sig = tok.split(".")
    forged = _b64(_claims(make_user(), roles=["admin"]))
    with pytest.raises(TokenError) as ei:
        decode_access_token(f"{head}.{forged}.{sig}", settings=s)
    assert ei.value.reason == "bad_signature"


def test_token_signed_with_another_secret_rejected():
    tok = jwt.encode(_claims(make_user()), "x" * 64, algorithm="HS256")
    with pytest.raises(TokenError, match="bad_signature"):
        decode_access_token(tok, settings=get_settings())


def test_alg_none_rejected():
    tok = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(_claims(make_user()))}."
    with pytest.raises(TokenError, match="algorithm_not_allowed"):
        decode_access_token(tok, settings=get_settings())


@pytest.mark.parametrize("alg", ["HS512", "RS256", "ES256", "None", "hs256"])
def test_other_algorithms_rejected_before_verification(alg):
    tok = f"{_b64({'alg': alg, 'typ': 'JWT'})}.{_b64(_claims(make_user()))}.c2ln"
    with pytest.raises(TokenError, match="algorithm_not_allowed"):
        decode_access_token(tok, settings=get_settings())


@pytest.mark.parametrize("tok", ["", "abc", "a.b", "a.b.c", "x" * 5000])
def test_garbage_tokens_rejected(tok):
    with pytest.raises(TokenError):
        decode_access_token(tok, settings=get_settings())


# ---------------- get_current_user through /auth/me ----------------
def test_me_returns_identity_from_db(client, repo):
    u = make_user(roles=("finance", "manager"))
    repo.put(u)
    r = client.get("/auth/me", headers=bearer(token_for(u)))
    assert r.status_code == 200, r.text
    assert r.json() == {
        "id": str(u.id),
        "tenant_id": str(u.tenant_id),
        "roles": ["finance", "manager"],
    }


def test_roles_are_reread_from_db_not_from_token(client, repo):
    u = make_user(roles=("admin",))
    repo.put(u)
    tok = token_for(u)  # token says admin
    repo.put(make_user(id=u.id, roles=("employee",)))  # demoted after issuance
    assert client.get("/auth/me", headers=bearer(tok)).json()["roles"] == ["employee"]


def test_deactivated_after_issuance_is_rejected(client, repo):
    u = make_user()
    repo.put(u)
    tok = token_for(u)
    repo.put(make_user(id=u.id, is_active=False))
    assert client.get("/auth/me", headers=bearer(tok)).status_code == 401


def test_deleted_user_is_rejected(client, repo):
    u = make_user()
    tok = token_for(u)  # never stored in the repo
    assert client.get("/auth/me", headers=bearer(tok)).status_code == 401


def test_token_tenant_must_match_db_tenant(client, repo, caplog):
    u = make_user()
    repo.put(u)
    tok = token_for(make_user(id=u.id, tenant_id=T2))  # validly signed, claims another tenant
    with caplog.at_level(logging.WARNING, logger="app.security"):
        r = client.get("/auth/me", headers=bearer(tok))
    assert r.status_code == 401 and "tenant_mismatch" in caplog.text


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic dXNlcjpwYXNz"}, {"Authorization": "Bearer"}, bearer("nope")],
)
def test_missing_or_malformed_credentials_get_401_with_challenge(client, repo, headers):
    r = client.get("/auth/me", headers=headers)
    assert r.status_code in (401, 403) and r.status_code == 401
    assert r.headers.get("www-authenticate", "").lower().startswith("bearer")


def test_expired_and_tampered_tokens_get_401(client, repo):
    u = make_user()
    repo.put(u)
    old = token_for(u, now=time.time() - 3600)
    head, body, sig = token_for(u).split(".")
    for tok in (old, f"{head}.{_b64(_claims(u, roles=['admin']))}.{sig}"):
        assert client.get("/auth/me", headers=bearer(tok)).status_code == 401


def test_rejection_reason_is_logged_but_not_returned(client, repo, caplog):
    with caplog.at_level(logging.WARNING, logger="app.security"):
        r = client.get("/auth/me", headers=bearer("x.y.z"))
    assert r.json() == {"detail": "not authenticated"}
    assert "auth_rejected reason=" in caplog.text and "x.y.z" not in caplog.text


# ---------------- /auth/login ----------------
def test_login_success_returns_working_token(client, repo):
    u = make_user()
    repo.put(u)
    r = client.post("/auth/login", json={"email": "Employee_1@ACME.example ", "password": PASSWORD})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer" and body["expires_in"] == 900
    assert client.get("/auth/me", headers=bearer(body["access_token"])).json()["id"] == str(u.id)


def test_login_failures_are_indistinguishable(client, repo, caplog):
    repo.put(make_user())
    repo.put(make_user(email="off@acme.example", is_active=False))
    cases = {
        "bad_password": {"email": "employee_1@acme.example", "password": "wrong"},
        "unknown_email": {"email": "nobody@acme.example", "password": PASSWORD},
        "inactive": {"email": "off@acme.example", "password": PASSWORD},
    }
    with caplog.at_level(logging.WARNING, logger="app.security"):
        responses = {k: client.post("/auth/login", json=v) for k, v in cases.items()}
    assert {r.status_code for r in responses.values()} == {401}
    assert len({r.text for r in responses.values()}) == 1  # identical bodies
    for reason in cases:
        assert f"login_failed reason={reason}" in caplog.text
    assert PASSWORD not in caplog.text and "nobody@acme.example" not in caplog.text


def test_unknown_email_still_runs_a_password_verification(client, repo, monkeypatch):
    calls = []
    import app.auth.router as router_mod

    monkeypatch.setattr(router_mod, "verify_password", lambda h, p: calls.append(h) or False)
    client.post("/auth/login", json={"email": "ghost@acme.example", "password": "pw"})
    assert len(calls) == 1 and calls[0].startswith("$argon2")


def test_login_rejects_extra_fields(client, repo):
    r = client.post(
        "/auth/login",
        json={"email": "a@b.example", "password": "x", "tenant_id": str(uuid.uuid4())},
    )
    assert r.status_code == 422


def test_verify_password_never_raises():
    assert verify_password("not-a-hash", "pw") is False
    assert verify_password(make_user().password_hash, "x" * 5000) is False
    assert verify_password(make_user().password_hash, "") is False


# ---------------- /ask protection ----------------
def test_ask_without_token_is_401(client, repo):
    assert client.post("/ask", json={"question": "hi"}).status_code == 401


@pytest.mark.parametrize(
    "extra", [{"tenant_id": str(uuid.uuid4())}, {"roles": ["admin"]}, {"user_id": "u1"}]
)
def test_ask_body_with_identity_fields_is_422_and_logged(client, repo, caplog, extra):
    u = make_user()
    repo.put(u)
    with caplog.at_level(logging.WARNING, logger="app.security"):
        r = client.post("/ask", json={"question": "hi", **extra}, headers=bearer(token_for(u)))
    assert r.status_code == 422
    assert "suspicious_request reason=forbidden_body_fields" in caplog.text
    assert next(iter(extra)) in caplog.text


def test_identity_headers_and_query_are_ignored_but_logged(client, repo, caplog):
    u = make_user()
    repo.put(u)
    with caplog.at_level(logging.WARNING, logger="app.security"):
        r = client.get(
            "/auth/me?tenant_id=" + str(T2),
            headers={**bearer(token_for(u)), "X-Tenant-Id": str(T2), "X-Roles": "admin"},
        )
    assert r.status_code == 200 and r.json()["tenant_id"] == str(u.tenant_id)
    assert r.json()["roles"] == ["employee"]
    assert "client_supplied_identity" in caplog.text


def test_health_and_login_are_public(client, repo):
    assert (
        client.post("/auth/login", json={"email": "a@b.example", "password": "x"}).status_code
        == 401
    )
    assert client.get("/health").status_code in (200, 503)  # public; DB may be unreachable here


# ---------------------------------------------------------------- Phase 7: token version
@pytest.mark.parametrize(
    "tv,reason",
    [
        (None, "missing_claim"),
        (0, "invalid_claims"),
        ("1", "invalid_claims"),
        (True, "invalid_claims"),
    ],
)
def test_tv_claim_required_and_strict(tv, reason):
    s = get_settings()
    claims = _claims(make_user())
    if tv is None:
        claims.pop("tv")
    else:
        claims["tv"] = tv
    with pytest.raises(TokenError) as e:
        decode_access_token(jwt.encode(claims, s.jwt_secret, algorithm="HS256"), settings=s)
    assert e.value.reason == reason


def test_token_with_old_version_is_rejected(client, repo):
    """A bumped token_version (logout-all / password change) kills every older token."""
    from dataclasses import replace

    user = make_user(token_version=3)
    repo.put(user)
    old = token_for(user, token_version=2)
    cur = token_for(user, token_version=3)
    assert client.get("/auth/me", headers=bearer(old)).status_code == 401
    assert client.get("/auth/me", headers=bearer(cur)).status_code == 200
    repo.put(replace(user, token_version=4))
    assert client.get("/auth/me", headers=bearer(cur)).status_code == 401


def test_login_issues_current_token_version(client, repo):
    user = make_user(token_version=7)
    repo.put(user)
    r = client.post("/auth/login", json={"email": user.email, "password": PASSWORD})
    s = get_settings()
    assert decode_access_token(r.json()["access_token"], settings=s).tv == 7
