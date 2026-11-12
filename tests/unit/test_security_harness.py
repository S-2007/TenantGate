"""The leak-suite harness itself: it must never let an unexercised attack count as a pass."""

from __future__ import annotations

import json

import httpx

from tests.security import runner
from tests.security.oracle import build_oracle


def test_tokens_are_refreshed_before_they_expire(monkeypatch):
    logins = []

    def handler(req: httpx.Request) -> httpx.Response:
        logins.append(json.loads(req.content)["email"])
        return httpx.Response(200, json={"access_token": f"t{len(logins)}", "expires_in": 900})

    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(runner.time, "time", lambda: clock["t"])
    tokens = runner.Tokens(httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler)),
                           build_oracle())  # fmt: skip
    assert tokens.login("acme:hr_1") == "t1"
    clock["t"] += runner.Tokens.REFRESH_S - 1
    assert tokens.login("acme:hr_1") == "t1"  # still fresh: cached
    clock["t"] += 2
    assert tokens.login("acme:hr_1") == "t2"  # older than REFRESH_S: re-login
    assert runner.Tokens.REFRESH_S < 15 * 60  # always below the token lifetime


def test_a_non_200_ask_is_a_failure_not_a_silent_pass():
    persona = build_oracle().personas["acme:employee_1"]
    problems = runner._check_ask(persona, httpx.Response(401, json={"detail": "not authenticated"}))
    assert problems and "not exercised" in problems[0]
    assert not runner.is_leak(problems[0])  # counted as a failure, which also fails the suite


def test_leak_and_failure_classification():
    assert runner.is_leak("retrieved forbidden doc x")
    assert runner.is_leak("canary 'X' in response")
    assert runner.is_leak("404 body differs (existence leak)")
    assert not runner.is_leak("expected (404,), got 401")


def test_own_uploads_exception_is_narrow():
    """D-045: only the persona's OWN uploads become listable metadata; nothing else is relaxed."""
    import uuid

    from app.auth.policy import AuthUser
    from tests.security.oracle import Persona, find_leaks

    u = AuthUser(id=uuid.uuid4(), tenant_id=uuid.uuid4(), roles=frozenset({"admin"}))
    p = Persona(key="a:admin", email="x", user=u, tenant="a", allowed_docs=frozenset({"pub"}),
                forbidden_docs=frozenset({"mine-hr", "other-co"}), forbidden_canaries=("SECRET-1",),
                forbidden_titles=("Alpha HR Salary Review", "Beta Co Forecast"),
                uploaded_docs=frozenset({"mine-hr"}))  # fmt: skip
    titles = {"mine-hr": "Alpha HR Salary Review", "other-co": "Beta Co Forecast"}
    q = p.for_own_uploads_listing(titles)
    text = "mine-hr Alpha HR Salary Review"
    assert find_leaks(q, retrieved_docs=(), cited_docs=(), text=text) == []
    # another company's doc, its title and any canary are still leaks
    leaks = find_leaks(
        q, retrieved_docs=(), cited_docs=(), text="other-co Beta Co Forecast SECRET-1"
    )
    assert len(leaks) == 3
    # and the original persona (used for /ask and every other endpoint) is unchanged
    assert find_leaks(p, retrieved_docs=(), cited_docs=(), text=text)


def test_audit_check_only_counts_ask_requests():
    from tests.security.runner import Finding

    assert Finding("f", "a", "p", 200, []).path == "/ask"
    assert Finding("f", "a", "p", 200, [], path="/documents/mine").path != "/ask"
