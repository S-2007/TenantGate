"""Shared test setup.

Safety rules applied BEFORE the app is imported:
- The app under test is pointed at TEST_DATABASE_URL(_DIRECT); the dev URLs are overwritten.
- LLM and judge use the deterministic FAKE provider; embeddings use the offline hash embedder.
- Neon-backed tests (fixture ``neon_db``) are skipped locally when no test URL is configured, but
  FAIL in CI (a silently skipped security suite would be worse than a red build).
- Before any Neon test runs, the test branch is verified non-default through the Neon API.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

from app.config import Settings  # noqa: E402

_file_settings = Settings()  # reads .env + real environment
_TEST_URL = _file_settings.test_database_url
_TEST_URL_DIRECT = _file_settings.test_database_url_direct
_UNUSED_URL = "postgresql://unused@db.invalid/none"  # .invalid never resolves

os.environ.update(
    {
        "ENV": "ci" if os.environ.get("CI") else "test",
        "LLM_PROVIDER": "fake",
        "EMBEDDING_MODEL": "hash",
        "DATABASE_URL": _TEST_URL or _UNUSED_URL,
        "DATABASE_URL_DIRECT": _TEST_URL_DIRECT or _UNUSED_URL,
        "JWT_SECRET": os.environ.get("JWT_SECRET") or "test-only-secret-" + "x" * 32,
        # Phase 6: registration on for tests; rate limits high (tests/unit/test_tenants.py
        # exercises the limiter itself with low limits)
        "REGISTRATION_ENABLED": "true",
        "REGISTRATION_CODE": "",
        "LOGIN_RATE_PER_MINUTE": "100000",
        "REGISTER_RATE_PER_HOUR": "100000",
        "UPLOAD_RATE_PER_MINUTE": "100000",
    }
)

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

IN_CI = bool(os.environ.get("CI"))


@pytest.fixture(scope="session")
def neon_db() -> str:
    """Verified, migrated Neon TEST branch. Returns the branch name."""
    s = get_settings()
    if not (_TEST_URL and _TEST_URL_DIRECT):
        msg = "TEST_DATABASE_URL / TEST_DATABASE_URL_DIRECT not set"
        if IN_CI:
            pytest.fail(msg + " (required in CI)")
        pytest.skip(msg + " - Neon-backed test skipped locally")
    from app.db.guard import assert_not_production

    branch = assert_not_production(
        _TEST_URL, neon_api_key=s.neon_api_key, neon_project_id=s.neon_project_id
    )
    assert_not_production(
        _TEST_URL_DIRECT, neon_api_key=s.neon_api_key, neon_project_id=s.neon_project_id
    )
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.cmd_opts = argparse.Namespace(x=["db=test"])
    command.upgrade(cfg, "head")
    return branch


TEST_ROLE_PREFIX = "rag_app_t_"


@pytest.fixture(scope="session")
def app_role(neon_db):
    """Point the app (DATABASE_URL) at a temporary low-privilege login role on the test branch.

    Migrations, seeding and fixtures run as the owner (DATABASE_URL_DIRECT); the code under test
    runs as a member of ``rag_app`` exactly like production. Yields the login role name; drops
    it afterwards (and any left over from crashed runs before).
    """
    import secrets

    from sqlalchemy import text

    from app.admin.roles import app_url, drop_login_role, new_password, upsert_login_role
    from app.api import get_rag_service
    from app.db.session import admin_transaction, reset_admin_engine, reset_engine

    name = TEST_ROLE_PREFIX + secrets.token_hex(4)
    password = new_password()
    with admin_transaction() as s:
        stale = s.execute(
            text("SELECT rolname FROM pg_roles WHERE rolname LIKE :p"),
            {"p": TEST_ROLE_PREFIX + "%"},
        ).scalars()
        for old in list(stale):
            drop_login_role(s, old)
        upsert_login_role(s, name, password)
    os.environ["DATABASE_URL"] = app_url(_TEST_URL, name, password)
    get_settings.cache_clear()
    get_rag_service.cache_clear()
    reset_engine()
    yield name
    reset_engine()
    with admin_transaction() as s:
        drop_login_role(s, name)
    reset_admin_engine()


@pytest.fixture()
def client():
    """FastAPI TestClient with lifespan (startup config validation) enabled."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
