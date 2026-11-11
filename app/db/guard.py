"""Guard that refuses to run tests / experiments against the Neon production (default) branch.

Fail-closed: the target branch must be *positively verified* as non-default through the Neon API
(NEON_API_KEY + NEON_PROJECT_ID). If verification is impossible, we abort rather than guess.
"""

from __future__ import annotations

import httpx

from app.config import ConfigError, url_host

NEON_API = "https://console.neon.tech/api/v2"
PRODUCTION_BRANCH_NAMES = {"main", "master", "prod", "production"}


def endpoint_id_from_host(host: str) -> str:
    """``ep-cool-name-123456-pooler.us-east-2.aws.neon.tech`` -> ``ep-cool-name-123456``."""
    label = host.split(".")[0]
    return label.removesuffix("-pooler")


def check_branch_is_safe(branch: dict) -> None:
    """Raise ConfigError if a Neon branch object looks like production."""
    name = str(branch.get("name", "")).lower()
    if branch.get("default") or branch.get("primary") or name in PRODUCTION_BRANCH_NAMES:
        raise ConfigError(
            f"Refusing to run: target database is on Neon branch '{branch.get('name')}', "
            "which is the default/production branch. Point TEST_DATABASE_URL at a dev/test branch."
        )


def assert_not_production(
    db_url: str,
    *,
    neon_api_key: str,
    neon_project_id: str,
    client: httpx.Client | None = None,
) -> str:
    """Verify ``db_url`` belongs to a non-default branch of the project. Returns the branch name.

    Raises ConfigError (never including the URL or key) on any doubt.
    """
    host = url_host(db_url)
    if not host:
        raise ConfigError("Test database URL has no host")
    if not (neon_api_key and neon_project_id):
        raise ConfigError(
            "Cannot verify that the test database is not production: set NEON_API_KEY and "
            "NEON_PROJECT_ID in .env (the guard fails closed)."
        )
    ep_id = endpoint_id_from_host(host)
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        headers = {"Authorization": f"Bearer {neon_api_key}", "Accept": "application/json"}
        base = f"{NEON_API}/projects/{neon_project_id}"
        r = client.get(f"{base}/endpoints", headers=headers)
        if r.status_code != 200:
            raise ConfigError(f"Neon API endpoint lookup failed (HTTP {r.status_code})")
        endpoints = r.json().get("endpoints", [])
        match = next((e for e in endpoints if e.get("id") == ep_id), None)
        if match is None:
            raise ConfigError(
                f"Endpoint {ep_id} not found in NEON_PROJECT_ID; refusing to run against an "
                "unverified database"
            )
        r = client.get(f"{base}/branches/{match['branch_id']}", headers=headers)
        if r.status_code != 200:
            raise ConfigError(f"Neon API branch lookup failed (HTTP {r.status_code})")
        branch = r.json().get("branch", {})
        check_branch_is_safe(branch)
        return str(branch.get("name"))
    finally:
        if own_client:
            client.close()
