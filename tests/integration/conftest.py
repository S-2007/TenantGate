"""Fixtures shared by the Neon integration tests."""

from __future__ import annotations

import pytest

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
