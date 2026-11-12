"""Fixtures shared by the Neon integration tests."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db.session import admin_transaction, reset_engine
from tests.integration import world


@pytest.fixture()
def seeded(app_role):
    """Fresh copy of the two-tenant test world; removed again afterwards."""
    with admin_transaction() as s:
        world.cleanup(s)
    with admin_transaction() as s:
        world.seed(s)
    yield world
    reset_engine()
    with admin_transaction() as s:
        world.cleanup(s)


@pytest.fixture()
def ingested(seeded):
    """Test world with every test document ingested (hash embedder)."""
    from app.ingestion.embedder import HashEmbedder
    from app.ingestion.pipeline import ingest

    with admin_transaction() as s:
        ingest(s, world.docs(), HashEmbedder(384), size=60, overlap=10)
    return world


# ---- Phase 6: self-service tenants created by tests (slug prefix) ----
PREFIX = "itest-ss-"


def purge_self_service(prefix: str = PREFIX) -> None:
    """Remove every tenant whose slug starts with ``prefix`` (and its users and documents)."""
    with admin_transaction() as s:
        ids = (
            s.execute(text("SELECT id FROM tenants WHERE slug LIKE :p"), {"p": prefix + "%"})
            .scalars()
            .all()
        )
        if ids:
            s.execute(text("DELETE FROM documents WHERE tenant_id = ANY(:t)"), {"t": ids})
            s.execute(text("DELETE FROM users WHERE tenant_id = ANY(:t)"), {"t": ids})
            s.execute(text("DELETE FROM tenants WHERE id = ANY(:t)"), {"t": ids})


@pytest.fixture()
def fresh(seeded):
    purge_self_service()
    yield
    purge_self_service()
