"""Baseline: pgvector extension + plain chunks table with HNSW cosine index.

Revision ID: 0001
Revises:
Create Date: 2026-11-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep in sync with app.config.Settings.embedding_dim and app.db.models.EMBEDDING_DIM
# (enforced by tests/unit/test_config.py). Changing the model dimension requires a new migration.
EMBEDDING_DIM = 384
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        f"""
        CREATE TABLE chunks (
            chunk_id    uuid PRIMARY KEY,
            doc_id      text NOT NULL,
            title       text NOT NULL,
            chunk_index integer NOT NULL CHECK (chunk_index >= 0),
            text        text NOT NULL CHECK (length(btrim(text)) > 0),
            embedding   vector({EMBEDDING_DIM}) NOT NULL,
            UNIQUE (doc_id, chunk_index)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops) "
        f"WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION})"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS chunks")
    # The extension is left installed: other objects may depend on it.
