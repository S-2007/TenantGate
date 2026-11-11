"""Eval http mode logs in as each question's persona (and re-uses the token)."""

from __future__ import annotations

import json

import httpx
import pytest

from eval.run_eval import PersonaLogins, persona_key


@pytest.mark.parametrize(
    "tenant,persona,key",
    [
        ("acme", "employee", "acme:employee_1"),
        ("lumen", "finance", "lumen:finance_1"),
        ("acme", "user:acme:employee_1", "acme:employee_1"),
    ],
)
def test_persona_key(tenant, persona, key):
    assert persona_key(tenant, persona) == key


def test_persona_logins_caches_tokens_and_never_sends_identity_in_body(tmp_path):
    creds = tmp_path / "seed_users.json"
    creds.write_text(
        json.dumps(
            {
                "password": "pw",
                "users": [
                    {"key": "acme:employee_1", "email": "employee_1@acme.example"},
                    {"key": "acme:finance_1", "email": "finance_1@acme.example"},
                ],
            }
        )
    )
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        assert set(body) == {"email", "password"}
        return httpx.Response(200, json={"access_token": f"tok-{body['email']}", "expires_in": 900})

    client = httpx.Client(base_url="http://t", transport=httpx.MockTransport(handler))
    logins = PersonaLogins(client, creds)
    h1 = logins.headers("acme", "employee")
    h2 = logins.headers("acme", "employee")
    h3 = logins.headers("acme", "finance")
    assert h1 == h2 == {"Authorization": "Bearer tok-employee_1@acme.example"}
    assert h3["Authorization"].endswith("finance_1@acme.example")
    assert len(seen) == 2  # one login per persona


def test_persona_logins_requires_seed_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="scripts.seed"):
        PersonaLogins(httpx.Client(), tmp_path / "missing.json")
