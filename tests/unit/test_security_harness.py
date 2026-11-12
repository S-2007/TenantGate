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
