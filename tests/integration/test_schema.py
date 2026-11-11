"""Database-level guarantees: least-privilege app role and constraints that make bad rows impossible."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.admin.roles import role_privileges
from app.db.session import admin_transaction, transaction

pytestmark = pytest.mark.neon

ZERO_VEC = "[" + ",".join(["0.1"] * 384) + "]"


# ---------------- the app role ----------------
def test_app_role_attributes_are_low_privilege(app_role):
    with transaction() as s:
        p = role_privileges(s)
    assert p["role"] == app_role and p["in_app_group"] is True
    assert (p["superuser"], p["bypass_rls"], p["create_role"], p["create_db"]) == (
        False,
        False,
        False,
        False,
    )
    assert p["owned_tables"] == 0


def test_app_role_can_read_the_four_tables(app_role):
    with transaction() as s:
        for table in ("tenants", "users", "documents", "chunks"):
            s.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()  # noqa: S608


@pytest.mark.parametrize(
    "stmt",
    [
        "ALTER TABLE chunks ADD COLUMN pwned int",
        "ALTER TABLE documents DROP CONSTRAINT ck_documents_acl_shape",
        "DROP TABLE chunks",
        "DROP TABLE users",
        "TRUNCATE chunks",
        "CREATE TABLE public.pwned (x int)",
        "CREATE INDEX pwned ON chunks (doc_id)",
        "ALTER ROLE CURRENT_USER BYPASSRLS",
        "CREATE ROLE pwned_role",
        "CREATE SCHEMA pwned",
    ],
)
def test_app_role_cannot_change_schema_or_roles(app_role, stmt):
    with pytest.raises(DBAPIError) as ei, transaction() as s:
        s.execute(text(stmt))
    assert "permission denied" in str(ei.value).lower() or "must be owner" in str(ei.value).lower()


@pytest.mark.parametrize(
    "stmt",
    [
        "DELETE FROM chunks",
        "UPDATE documents SET acl_version = acl_version + 1",
        "UPDATE users SET roles = ARRAY['admin']",
        "INSERT INTO tenants (id, slug, name) VALUES (gen_random_uuid(), 'pwned', 'x')",
    ],
)
def test_app_role_cannot_write(app_role, stmt):
    with pytest.raises(DBAPIError, match="permission denied"), transaction() as s:
        s.execute(text(stmt))


# ---------------- constraints (inserted as the owner, which can write) ----------------
def _chunk_sql(**over) -> tuple[str, dict]:
    vals = {
        "chunk_id": uuid.uuid4(),
        "doc_id": "itest-a-salary",
        "tenant_id": None,
        "title": "t",
        "chunk_index": 900,
        "text": "x",
        "classification": "confidential",
        "allowed_roles": ["hr"],
        "allowed_users": [],
        "acl_version": 1,
        "embedding": ZERO_VEC,
    } | over
    sql = (
        "INSERT INTO chunks (chunk_id, doc_id, tenant_id, title, chunk_index, text, embedding, "
        "classification, allowed_roles, allowed_users, acl_version) VALUES (:chunk_id, :doc_id, "
        ":tenant_id, :title, :chunk_index, :text, CAST(:embedding AS vector), "
        "CAST(:classification AS doc_classification), CAST(:allowed_roles AS text[]), "
        "CAST(:allowed_users AS uuid[]), :acl_version)"
    )
    return sql, vals


def _insert_fails(match: str, **over) -> None:
    sql, vals = _chunk_sql(**over)
    with pytest.raises(IntegrityError, match=match), admin_transaction() as s:
        s.execute(text(sql), vals)


def test_valid_chunk_is_accepted_and_visibility_is_derived(seeded):
    sql, vals = _chunk_sql(tenant_id=seeded.tid("itest-a"))
    with admin_transaction() as s:
        s.execute(text(sql), vals)
        vis = s.execute(
            text("SELECT visibility FROM chunks WHERE chunk_id = :c"), {"c": vals["chunk_id"]}
        ).scalar_one()
    assert vis == "restricted"


def test_db_rejects_chunk_without_tenant(seeded):
    _insert_fails("tenant_id", tenant_id=None)


def test_db_rejects_confidential_chunk_with_empty_acl(seeded):
    _insert_fails("ck_chunks_acl_shape", tenant_id=seeded.tid("itest-a"), allowed_roles=[])


def test_db_rejects_public_or_internal_chunk_with_acl_lists(seeded):
    _insert_fails(
        "ck_chunks_acl_shape",
        tenant_id=seeded.tid("itest-a"),
        doc_id="itest-a-leave",
        classification="internal",
        allowed_roles=["hr"],
    )


def test_db_rejects_chunk_claiming_another_tenant_than_its_document(seeded):
    _insert_fails("fk_chunks_document_same_tenant", tenant_id=seeded.tid("itest-b"))


def test_db_rejects_chunk_without_document(seeded):
    _insert_fails(
        "fk_chunks_document_same_tenant", tenant_id=seeded.tid("itest-a"), doc_id="itest-a-nope"
    )


def test_db_rejects_unknown_role_in_chunk_acl(seeded):
    _insert_fails("check", tenant_id=seeded.tid("itest-a"), allowed_roles=["root"])


def test_db_rejects_confidential_document_nobody_can_read(seeded):
    with pytest.raises(IntegrityError, match="ck_documents_acl_shape"), admin_transaction() as s:
        s.execute(
            text(
                "INSERT INTO documents (id, tenant_id, title, source_path, classification) "
                "VALUES ('itest-a-orphan', :t, 'x', 'x', 'confidential')"
            ),
            {"t": seeded.tid("itest-a")},
        )


def test_db_rejects_user_with_unknown_role_or_uppercase_email(seeded):
    base = (
        "INSERT INTO users (id, tenant_id, email, password_hash, roles) VALUES "
        "(gen_random_uuid(), :t, :e, '$argon2id$x', CAST(:r AS text[]))"
    )
    for email, roles in (("ok@itest-a.example", ["root"]), ("Upper@itest-a.example", ["hr"])):
        with pytest.raises(IntegrityError), admin_transaction() as s:
            s.execute(text(base), {"t": seeded.tid("itest-a"), "e": email, "r": roles})
