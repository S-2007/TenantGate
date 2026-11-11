"""Alembic environment: always uses the DIRECT (non-pooled) Neon connection.

Usage:
    alembic upgrade head              # DATABASE_URL_DIRECT (dev branch)
    alembic -x db=test upgrade head   # TEST_DATABASE_URL_DIRECT (guarded: never the default branch)
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings, normalize_pg_url
from app.db.guard import assert_not_production
from app.db.models import Base

config = context.config
target_metadata = Base.metadata


def _resolve_url() -> str:
    settings = get_settings()
    target = context.get_x_argument(as_dictionary=True).get("db", "dev")
    if target == "test":
        settings.require("test_database_url_direct")
        raw = settings.test_database_url_direct
        assert_not_production(
            raw, neon_api_key=settings.neon_api_key, neon_project_id=settings.neon_project_id
        )
        return normalize_pg_url(raw, var_name="TEST_DATABASE_URL_DIRECT")
    settings.validate_for_migrations()
    return settings.sqlalchemy_url_direct


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url="postgresql+psycopg://offline/offline",
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations over a single direct connection (no pool)."""
    engine = create_engine(_resolve_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
