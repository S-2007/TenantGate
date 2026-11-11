"""ACL-aware chunks: every chunk carries its tenant and a copy of its document's ACL.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-10

Guarantees enforced by the database (not just by ingestion code):
- tenant_id NOT NULL and (doc_id, tenant_id) must match a row in documents: a chunk can't exist
  without a document, and can't claim a different tenant than its document.
- the ACL shape rule of documents (public/internal: empty lists; confidential: someone named).
- ``visibility`` is a generated column derived from ``classification``, so it can't disagree.
The ACL *values* (roles, users, version) must equal the document's; that cross-table equality is
checked by ingestion (ChunkPayload) and audited by scripts/verify_index.py.

Existing Phase 1 chunks have no tenant or ACL and are derived data, so they are deleted here.
Re-run ``scripts.seed`` then ``scripts.ingest`` to rebuild the index.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KNOWN_ROLES = ("admin", "contractor", "employee", "engineering", "finance", "hr", "manager")
_ROLES_SQL = "ARRAY[" + ", ".join(f"'{r}'" for r in KNOWN_ROLES) + "]::text[]"


def upgrade() -> None:
    op.execute("DELETE FROM chunks")  # Phase 1 rows: no tenant/ACL, rebuilt by re-ingest
    op.execute(
        f"""
        ALTER TABLE chunks
            ADD COLUMN tenant_id uuid NOT NULL
                REFERENCES tenants(id) ON DELETE RESTRICT,
            ADD COLUMN classification doc_classification NOT NULL,
            ADD COLUMN visibility text GENERATED ALWAYS AS (
                CASE classification
                    WHEN 'public' THEN 'tenant'
                    WHEN 'internal' THEN 'employees'
                    ELSE 'restricted'
                END
            ) STORED,
            ADD COLUMN allowed_roles text[] NOT NULL
                CHECK (array_position(allowed_roles, NULL) IS NULL
                       AND allowed_roles <@ {_ROLES_SQL}),
            ADD COLUMN allowed_users uuid[] NOT NULL
                CHECK (array_position(allowed_users, NULL) IS NULL),
            ADD COLUMN acl_version integer NOT NULL CHECK (acl_version >= 1),
            ADD CONSTRAINT fk_chunks_document_same_tenant
                FOREIGN KEY (doc_id, tenant_id) REFERENCES documents (id, tenant_id)
                ON DELETE CASCADE,
            ADD CONSTRAINT ck_chunks_acl_shape CHECK (
                (classification IN ('public', 'internal')
                    AND cardinality(allowed_roles) = 0 AND cardinality(allowed_users) = 0)
                OR (classification = 'confidential'
                    AND (cardinality(allowed_roles) > 0 OR cardinality(allowed_users) > 0))
            )
        """
    )
    op.execute("CREATE INDEX ix_chunks_tenant_id ON chunks (tenant_id)")
    op.execute("CREATE INDEX ix_chunks_allowed_roles ON chunks USING gin (allowed_roles)")
    op.execute("CREATE INDEX ix_chunks_allowed_users ON chunks USING gin (allowed_users)")
    # doc_id lookups (stale-chunk delete, FK cascade) use the existing UNIQUE (doc_id,
    # chunk_index) btree, whose leading column is doc_id; a separate doc_id index would duplicate it.


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_allowed_users")
    op.execute("DROP INDEX IF EXISTS ix_chunks_allowed_roles")
    op.execute("DROP INDEX IF EXISTS ix_chunks_tenant_id")
    op.execute(
        """
        ALTER TABLE chunks
            DROP CONSTRAINT IF EXISTS ck_chunks_acl_shape,
            DROP CONSTRAINT IF EXISTS fk_chunks_document_same_tenant,
            DROP COLUMN IF EXISTS acl_version,
            DROP COLUMN IF EXISTS allowed_users,
            DROP COLUMN IF EXISTS allowed_roles,
            DROP COLUMN IF EXISTS visibility,
            DROP COLUMN IF EXISTS classification,
            DROP COLUMN IF EXISTS tenant_id
        """
    )
