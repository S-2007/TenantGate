"""Pure (no-DB) tests for ACL storage: ChunkPayload, seed planning, seed guards, role tooling."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.admin.roles import app_url, upsert_login_role
from app.admin.seed import SeedError, build_plan, email_for, tenant_uuid, user_uuid
from app.ingestion.corpus import load_manifest, validate_manifest
from app.ingestion.payload import ChunkPayload

T = uuid.uuid4()


def payload(**over) -> dict:  # type: ignore[no-untyped-def]
    return {
        "chunk_id": uuid.uuid4(),
        "doc_id": "acme-x",
        "tenant_id": T,
        "title": "X",
        "chunk_index": 0,
        "text": "hello",
        "embedding": tuple([0.1] * 384),
        "classification": "confidential",
        "allowed_roles": ("hr", "admin"),
        "allowed_users": (),
        "acl_version": 1,
    } | over


# ---------------- ChunkPayload ----------------
def test_valid_payload_sorts_acl_lists():
    p = ChunkPayload(**payload())
    assert p.allowed_roles == ("admin", "hr")
    assert "visibility" not in p.row() and len(p.row()["embedding"]) == 384


@pytest.mark.parametrize(
    "over,needle",
    [
        ({"tenant_id": None}, "tenant_id"),
        ({"allowed_roles": ()}, "confidential chunk needs"),
        ({"classification": "internal"}, "must have empty"),
        ({"classification": "secret"}, "classification"),
        ({"allowed_roles": ("root",)}, "unknown roles"),
        ({"allowed_roles": ("hr", "hr")}, "duplicate roles"),
        ({"embedding": tuple([0.1] * 383)}, "383 dims"),
        ({"embedding": tuple([float("nan")] * 384)}, "NaN"),
        ({"text": "   "}, "blank"),
        ({"chunk_index": -1}, "chunk_index"),
        ({"acl_version": 0}, "acl_version"),
        ({"extra_field": 1}, "extra"),
    ],
)
def test_invalid_payload_rejected(over, needle):
    with pytest.raises(ValidationError, match=needle):
        ChunkPayload(**payload(**over))


def test_payload_has_no_defaults_so_a_forgotten_acl_is_an_error():
    p = payload()
    for field in ("allowed_roles", "allowed_users", "acl_version", "classification", "tenant_id"):
        missing = {k: v for k, v in p.items() if k != field}
        with pytest.raises(ValidationError, match=field):
            ChunkPayload(**missing)


def test_public_payload_with_empty_acl_ok():
    ChunkPayload(**payload(classification="public", allowed_roles=()))


# ---------------- seed plan ----------------
def test_seed_plan_from_real_manifest():
    plan = build_plan(load_manifest("data/corpus"))
    assert len(plan.tenants) == 3 and len(plan.users) == 24 and len(plan.documents) == 66
    by_key = {u.key: u for u in plan.users}
    for t in ("acme", "northwind", "lumen"):
        tenant_users = [u for u in plan.users if u.key.startswith(t + ":")]
        assert 6 <= len(tenant_users) <= 8
        assert by_key[f"{t}:employee_2"].is_active is False  # the deactivated user
        assert by_key[f"{t}:multi_1"].roles == ("finance", "manager")  # the multi-role user
        assert {r for u in tenant_users for r in u.roles} >= {
            "employee",
            "manager",
            "hr",
            "finance",
            "engineering",
            "admin",
        }
    review = next(d for d in plan.documents if d.id == "acme-performance-review-employee-1")
    assert review.allowed_users == (user_uuid("acme:employee_1"),) and review.allowed_roles == ()
    for d in plan.documents:
        if d.classification in ("public", "internal"):
            assert d.allowed_roles == () and d.allowed_users == ()
        else:
            assert d.allowed_roles or d.allowed_users


def test_seed_ids_are_deterministic_and_emails_reserved():
    assert tenant_uuid("acme") == tenant_uuid("acme") != tenant_uuid("lumen")
    assert email_for("acme:hr_1") == "hr_1@acme.example"


def test_seed_plan_rejects_cross_tenant_user_grant():
    m = load_manifest("data/corpus")
    doc = next(d for d in m["documents"] if d.get("allowed_users"))
    doc["allowed_users"] = ["lumen:employee_1"] if doc["tenant"] != "lumen" else ["acme:employee_1"]
    with pytest.raises(SeedError, match="another tenant"):
        build_plan(m)


def test_manifest_validator_flags_acl_lists_on_internal_docs():
    m = load_manifest("data/corpus")
    internal = next(d for d in m["documents"] if d["classification"] == "internal")
    internal["allowed_roles"] = ["engineering"]
    assert any("must have empty" in p for p in validate_manifest(m, "data/corpus"))


# ---------------- seed CLI guards ----------------
def test_seed_refuses_in_production_before_touching_the_db(monkeypatch, capsys):
    import scripts.seed as seed_cli
    from app.config import get_settings

    def boom(*a, **k):  # any DB access would call this
        raise AssertionError("database touched")

    monkeypatch.setenv("ENV", "production")
    get_settings.cache_clear()
    monkeypatch.setattr("app.db.session.admin_transaction", boom)
    try:
        assert seed_cli.main([]) == 2
        assert "ENV=production" in capsys.readouterr().err
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_seed_validate_only_needs_no_database(capsys):
    import scripts.seed as seed_cli

    assert seed_cli.main(["--validate-only"]) == 0
    assert '"problems": []' in capsys.readouterr().out


# ---------------- login-role tooling ----------------
def test_generated_password_is_strong_and_inline_safe():
    from app.admin.roles import _SAFE_PASSWORD, new_password

    pw = new_password()
    assert len(pw) >= 43 and _SAFE_PASSWORD.match(pw)


@pytest.mark.parametrize("bad", ["short", "x" * 40 + "'; DROP ROLE x; --", "é" * 40])
def test_unsafe_password_rejected_before_any_sql(bad):
    class NoSession:
        def execute(self, *a, **k):
            raise AssertionError("SQL executed")

    with pytest.raises(ValueError, match="URL-safe"):
        upsert_login_role(NoSession(), "rag_app_ok", bad)  # type: ignore[arg-type]


def test_app_url_swaps_credentials_only():
    url = app_url(
        "postgresql://owner:secret@ep-x-pooler.aws.neon.tech/neondb?sslmode=require",
        "rag_app_login",
        "p/w+=",
    )
    assert url == (
        "postgresql://rag_app_login:p%2Fw%2B%3D@ep-x-pooler.aws.neon.tech/neondb?sslmode=require"
    )
    assert "secret" not in url and "owner" not in url


@pytest.mark.parametrize("bad", ["rag_app", "Robert'); DROP TABLE x;--", "1abc", "a" * 70, ""])
def test_login_role_name_is_validated_before_any_sql(bad):
    class NoSession:
        def execute(self, *a, **k):
            raise AssertionError("SQL executed")

    with pytest.raises(ValueError, match="invalid login role name"):
        upsert_login_role(NoSession(), bad, "pw")  # type: ignore[arg-type]
