"""Append-only retrieval audit log + tenant-scoped document existence check for the admin API.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-11

Append-only, three ways:
1. grants: rag_app gets INSERT and SELECT only (no UPDATE, DELETE, TRUNCATE);
2. a trigger raises on UPDATE, DELETE and TRUNCATE, even for the owner;
3. RLS: rag_app may only read its current tenant's rows, and may only insert rows for its
   current tenant (or unauthenticated rows with tenant NULL).
The owner can still drop the trigger. A tamper-proof copy needs an external WORM sink (D-030).

No foreign keys: audit rows must outlive the users, documents and tenants they mention.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rag_app"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE retrieval_audit (
            id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            request_id         uuid NOT NULL,
            ts                 timestamptz NOT NULL DEFAULT now(),
            event_type         text NOT NULL CHECK (event_type IN ('retrieval', 'acl_change')),
            user_id            uuid,
            tenant_id          uuid,
            roles              text[] NOT NULL DEFAULT '{}',
            query_text         text,
            query_hash         text CHECK (query_hash IS NULL OR query_hash ~ '^[0-9a-f]{64}$'),
            returned_chunk_ids uuid[] NOT NULL DEFAULT '{}',
            returned_doc_ids   text[] NOT NULL DEFAULT '{}',
            dropped_chunk_ids  uuid[] NOT NULL DEFAULT '{}',
            model              text,
            latency_ms         double precision CHECK (latency_ms IS NULL OR latency_ms >= 0),
            status             text NOT NULL CHECK (status IN ('ok', 'denied', 'error')),
            http_status        integer,
            -- an authenticated row always names both; an unauthenticated denial names neither
            CONSTRAINT ck_audit_identity CHECK ((user_id IS NULL) = (tenant_id IS NULL))
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_audit_returned_doc_ids ON retrieval_audit USING gin (returned_doc_ids)"
    )
    op.execute("CREATE INDEX ix_audit_tenant_ts ON retrieval_audit (tenant_id, ts DESC)")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION retrieval_audit_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'retrieval_audit is append-only (% blocked)', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_retrieval_audit_no_update_delete
        BEFORE UPDATE OR DELETE ON retrieval_audit
        FOR EACH ROW EXECUTE FUNCTION retrieval_audit_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_retrieval_audit_no_truncate
        BEFORE TRUNCATE ON retrieval_audit
        FOR EACH STATEMENT EXECUTE FUNCTION retrieval_audit_append_only()
        """
    )
    op.execute(f"REVOKE ALL ON retrieval_audit FROM {APP_ROLE}")
    op.execute(f"GRANT INSERT, SELECT ON retrieval_audit TO {APP_ROLE}")

    op.execute("ALTER TABLE retrieval_audit ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE retrieval_audit FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY audit_insert_own_tenant ON retrieval_audit FOR INSERT TO {APP_ROLE}
        WITH CHECK (tenant_id IS NULL OR tenant_id = app_tenant_id())
        """
    )
    op.execute(
        f"""
        CREATE POLICY audit_read_own_tenant ON retrieval_audit FOR SELECT TO {APP_ROLE}
        USING (tenant_id = app_tenant_id())
        """
    )

    # "Does this document exist IN MY tenant?" without exposing the document row through RLS
    # (an admin may audit a confidential document it cannot read). Returns only a boolean, and
    # only for the caller's own tenant, so it can't be used to probe other tenants.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION document_in_current_tenant(doc text) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT EXISTS (
                SELECT 1 FROM public.documents
                WHERE id = doc AND tenant_id = public.app_tenant_id()
            )
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION document_in_current_tenant(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION document_in_current_tenant(text) TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS document_in_current_tenant(text)")
    op.execute("DROP TABLE IF EXISTS retrieval_audit")  # drops its triggers and policies
    op.execute("DROP FUNCTION IF EXISTS retrieval_audit_append_only()")
