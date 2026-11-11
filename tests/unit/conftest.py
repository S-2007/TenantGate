"""Unit-test fixtures: the app with an in-memory audit sink (no database)."""

from __future__ import annotations

import pytest


@pytest.fixture()
def audit_rows():
    """Audit records 'written' during the test."""
    return []


@pytest.fixture()
def client(audit_rows):
    """TestClient whose audit writer appends to ``audit_rows`` instead of Postgres."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    app.state.audit_writer = audit_rows.append
    with TestClient(app) as c:
        yield c
