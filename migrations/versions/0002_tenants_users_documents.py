"""Access model: tenants, users, documents (ACL source of truth) + low-privilege app role.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-10

Roles (Postgres roles are per Neon branch and survive schema resets):
- OWNER = whoever runs migrations (Neon's ``neondb_owner`` via DATABASE_URL_DIRECT). Owns every
  table, so only it can ALTER/DROP.
- ``rag_app`` = NOLOGIN group role holding the app's grants: USAGE on the schema and SELECT on the
  four tables, nothing else (no INSERT/UPDATE/DELETE, no DDL, NOBYPASSRLS). Login roles that the
  running app uses are created as members of it by ``scripts/create_app_role.py`` (password stays
  out of migrations and out of git). It is created with SQL, not the Neon API, on purpose: roles
  made through the Neon console/API join ``neon_superuser``, which can bypass RLS.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "rag_app"
# Keep in sync with app.auth.policy.KNOWN_ROLES (tests/unit/test_schema_sync.py checks it).
KNOWN_ROLES = ("admin", "contractor", "employee", "engineering", "finance", "hr", "manager")
_ROLES_SQL = "ARRAY[" + ", ".join(f"'{r}'" for r in KNOWN_ROLES) + "]::text[]"
APP_SELECT_TABLES = ("tenants", "users", "documents", "chunks")


def upgrade() -> None:
    op.execute("CREATE TYPE doc_classification AS ENUM ('public', 'internal', 'confidential')")
    op.execute(
        """
        CREATE TABLE tenants (
            id   uuid PRIMARY KEY,
            slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
            name text NOT NULL CHECK (length(btrim(name)) > 0)
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE users (
            id            uuid PRIMARY KEY,
            tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            email         text NOT NULL UNIQUE
                          CHECK (email = lower(email) AND email LIKE '%_@_%'),
            password_hash text NOT NULL CHECK (password_hash LIKE '$argon2%'),
            roles         text[] NOT NULL
                          CHECK (cardinality(roles) > 0
                                 AND array_position(roles, NULL) IS NULL
                                 AND roles <@ {_ROLES_SQL}),
            is_active     boolean NOT NULL DEFAULT true,
            created_at    timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_users_tenant_id ON users (tenant_id)")
    op.execute(
        f"""
        CREATE TABLE documents (
            id             text PRIMARY KEY CHECK (length(btrim(id)) > 0),
            tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            title          text NOT NULL,
            source_path    text NOT NULL,
            classification doc_classification NOT NULL,
            allowed_roles  text[] NOT NULL DEFAULT '{{}}'
                           CHECK (array_position(allowed_roles, NULL) IS NULL
                                  AND allowed_roles <@ {_ROLES_SQL}),
            allowed_users  uuid[] NOT NULL DEFAULT '{{}}'
                           CHECK (array_position(allowed_users, NULL) IS NULL),
            acl_version    integer NOT NULL DEFAULT 1 CHECK (acl_version >= 1),
            created_at     timestamptz NOT NULL DEFAULT now(),
            updated_at     timestamptz NOT NULL DEFAULT now(),
            -- lets chunks reference (doc_id, tenant_id) so a chunk can't claim another tenant
            CONSTRAINT uq_documents_id_tenant UNIQUE (id, tenant_id),
            -- public/internal: semantics come from the classification, so no ACL lists;
            -- confidential: someone must be named, or nobody could ever read it
            CONSTRAINT ck_documents_acl_shape CHECK (
                (classification IN ('public', 'internal')
                    AND cardinality(allowed_roles) = 0 AND cardinality(allowed_users) = 0)
                OR (classification = 'confidential'
                    AND (cardinality(allowed_roles) > 0 OR cardinality(allowed_users) > 0))
            )
        )
        """
    )
    op.execute("CREATE INDEX ix_documents_tenant_id ON documents (tenant_id)")

    # Low-privilege app role. Idempotent: the role is per branch and outlives schema resets.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} NOLOGIN;
            END IF;
        END
        $$
        """  # noqa: S608 - APP_ROLE is a module constant, not input
    )
    # A non-superuser owner (Neon) may not even *state* NOSUPERUSER/NOREPLICATION/NOBYPASSRLS,
    # so the role is created with defaults (which are all "NO") and then asserted: if a
    # pre-existing role has any dangerous attribute, the migration aborts instead of granting.
    op.execute(f"ALTER ROLE {APP_ROLE} NOLOGIN NOCREATEDB NOCREATEROLE")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}'
                       AND (rolsuper OR rolbypassrls OR rolreplication OR rolcanlogin
                            OR rolcreaterole OR rolcreatedb)) THEN
                RAISE EXCEPTION 'role {APP_ROLE} has unsafe attributes; refusing to grant';
            END IF;
        END
        $$
        """  # noqa: S608 - APP_ROLE is a module constant, not input
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    for table in APP_SELECT_TABLES:
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
        op.execute(f"GRANT SELECT ON {table} TO {APP_ROLE}")


def downgrade() -> None:
    for table in APP_SELECT_TABLES:
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
    op.execute("DROP TABLE IF EXISTS documents")
    op.execute("DROP TABLE IF EXISTS users")
    op.execute("DROP TABLE IF EXISTS tenants")
    op.execute("DROP TYPE IF EXISTS doc_classification")
    # The role itself is kept: it is branch-wide and login roles may still be members of it.
