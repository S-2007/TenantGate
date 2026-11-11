"""Row-level security on chunks and documents, mirroring app.auth.policy.can_access.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-11

- ENABLE + FORCE RLS: the policies bind every role, including the table owner unless it has
  BYPASSRLS. Neon's ``neondb_owner`` has BYPASSRLS, which admin jobs (seed, ingest,
  verify_index) rely on. ``rag_app`` does not, and migration 0002 asserts that.
- Only ``rag_app`` (and its login members) get a policy, and it is SELECT-only. Any other role
  has no policy and therefore sees nothing.
- Identity comes from transaction-local settings (app.tenant_id / app.user_id / app.roles, set
  by app.db.rls.apply_rls_context). Missing settings -> NULL -> no rows (fail closed). A
  malformed tenant or user id raises a cast error (also no rows).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rag_app"
# Keep in sync with app.auth.policy.EMPLOYEE_ROLES (tests/unit/test_schema_sync.py).
EMPLOYEE_ROLES = ("admin", "employee", "engineering", "finance", "hr", "manager")
_EMP_SQL = "ARRAY[" + ", ".join(f"'{r}'" for r in EMPLOYEE_ROLES) + "]::text[]"

HELPERS = [
    """
    CREATE OR REPLACE FUNCTION app_tenant_id() RETURNS uuid
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT nullif(current_setting('app.tenant_id', true), '')::uuid $$
    """,
    """
    CREATE OR REPLACE FUNCTION app_user_id() RETURNS uuid
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$
    """,
    """
    CREATE OR REPLACE FUNCTION app_roles() RETURNS text[]
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT coalesce(string_to_array(nullif(current_setting('app.roles', true), ''), ','),
                          '{}'::text[]) $$
    """,
    f"""
    CREATE OR REPLACE FUNCTION app_is_employee() RETURNS boolean
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT app_roles() && {_EMP_SQL} $$
    """,
]

# visibility: tenant = public, employees = internal, restricted = confidential (migration 0003)
CHUNKS_POLICY = f"""
    CREATE POLICY chunks_tenant_acl ON chunks FOR SELECT TO {APP_ROLE}
    USING (
        tenant_id = app_tenant_id()
        AND (
            visibility = 'tenant'
            OR (visibility = 'employees' AND app_is_employee())
            OR (visibility = 'restricted'
                AND (allowed_roles && app_roles() OR app_user_id() = ANY (allowed_users)))
        )
    )
"""
DOCUMENTS_POLICY = f"""
    CREATE POLICY documents_tenant_acl ON documents FOR SELECT TO {APP_ROLE}
    USING (
        tenant_id = app_tenant_id()
        AND (
            classification = 'public'
            OR (classification = 'internal' AND app_is_employee())
            OR (classification = 'confidential'
                AND (allowed_roles && app_roles() OR app_user_id() = ANY (allowed_users)))
        )
    )
"""


def upgrade() -> None:
    for sql in HELPERS:
        op.execute(sql)
    for fn in ("app_tenant_id()", "app_user_id()", "app_roles()", "app_is_employee()"):
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO {APP_ROLE}")
    for table in ("chunks", "documents"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(CHUNKS_POLICY)
    op.execute(DOCUMENTS_POLICY)


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS chunks_tenant_acl ON chunks")
    op.execute("DROP POLICY IF EXISTS documents_tenant_acl ON documents")
    for table in ("chunks", "documents"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    for fn in ("app_is_employee()", "app_roles()", "app_user_id()", "app_tenant_id()"):
        op.execute(f"DROP FUNCTION IF EXISTS {fn}")
