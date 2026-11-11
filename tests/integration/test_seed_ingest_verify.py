"""Seed idempotency, ACL-aware ingestion and the index audit, against the Neon test branch."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text, update

from app.admin.seed import apply_plan, build_plan
from app.admin.verify import verify_index
from app.db.models import Chunk, Document, Tenant, User
from app.db.session import admin_transaction
from app.ingestion.corpus import load_manifest
from app.ingestion.embedder import HashEmbedder
from app.ingestion.pipeline import IngestError, count_chunks, ingest
from tests.integration import world

pytestmark = pytest.mark.neon
EMB = HashEmbedder(384)


def _counts(s) -> dict[str, int]:  # type: ignore[no-untyped-def]
    return {
        m.__tablename__: int(s.execute(select(func.count()).select_from(m)).scalar_one())
        for m in (Tenant, User, Document)
    }


# ---------------- seed ----------------
def test_real_manifest_seed_is_idempotent(app_role):
    plan = build_plan(load_manifest("data/corpus"))
    with admin_transaction() as s:
        apply_plan(s, plan, "real-seed-test-pw")
    with admin_transaction() as s:
        before = _counts(s)
        second = apply_plan(s, plan, "real-seed-test-pw")
        after = _counts(s)
    assert before == after
    assert second.tenants.inserted == second.users.inserted == second.documents.inserted == 0
    assert second.users.updated == second.documents.updated == second.acl_version_bumps == 0
    assert second.users.unchanged == 24 and second.documents.unchanged == 66


def test_acl_change_bumps_acl_version_and_metadata_change_does_not(seeded):
    m = {**world.MANIFEST, "documents": [dict(d) for d in world.MANIFEST["documents"]]}
    m["documents"][1]["allowed_roles"] = ["hr", "finance"]  # itest-a-salary
    m["documents"][0]["title"] = "Itest A Leave Policy v2"  # itest-a-leave
    with admin_transaction() as s:
        stats = apply_plan(s, build_plan(m), world.PASSWORD)
        salary = s.get(Document, "itest-a-salary")
        leave = s.get(Document, "itest-a-leave")
    assert stats.acl_version_bumps == 1 and stats.documents.updated == 2
    assert salary.acl_version == 2 and sorted(salary.allowed_roles) == ["finance", "hr"]
    assert leave.acl_version == 1 and leave.title.endswith("v2")


# ---------------- ingest ----------------
def test_ingest_copies_document_acl_onto_every_chunk(ingested):
    with admin_transaction() as s:
        rows = s.execute(select(Chunk, Document).join(Document, Document.id == Chunk.doc_id)).all()
        mine = [(c, d) for c, d in rows if c.doc_id.startswith("itest-")]
    assert len(mine) > 4
    for c, d in mine:
        assert (c.tenant_id, c.classification, c.acl_version) == (
            d.tenant_id,
            d.classification,
            d.acl_version,
        )
        assert sorted(c.allowed_roles) == sorted(d.allowed_roles)
        assert sorted(map(str, c.allowed_users)) == sorted(map(str, d.allowed_users))
    review = [c for c, _ in mine if c.doc_id == "itest-a-review"]
    assert review and review[0].allowed_users == [world.uid("itest-a:employee_1")]


def test_reingest_is_idempotent(ingested):
    with admin_transaction() as s:
        ids1 = sorted(
            map(str, s.scalars(select(Chunk.chunk_id).where(Chunk.doc_id.like("itest-%"))))
        )
        stats = ingest(s, world.docs(), EMB, size=60, overlap=10)
        ids2 = sorted(
            map(str, s.scalars(select(Chunk.chunk_id).where(Chunk.doc_id.like("itest-%"))))
        )
    assert stats.inserted == 0 and stats.updated == stats.chunks == len(ids1)
    assert ids1 == ids2


def test_shrinking_document_removes_stale_chunks(ingested):
    shrunk = world.docs("itest-a-salary")
    shrunk = [type(shrunk[0])(**{**shrunk[0].__dict__, "text": "Now a short note."})]
    with admin_transaction() as s:
        before = count_chunks(s, ["itest-a-salary"])
        stats = ingest(s, shrunk, EMB, size=60, overlap=10)
        after = count_chunks(s, ["itest-a-salary"])
    assert before > 1 and after == 1 and stats.stale_deleted == before - 1


def test_ingest_picks_up_acl_change_on_reingest(ingested):
    with admin_transaction() as s:
        s.execute(
            update(Document)
            .where(Document.id == "itest-a-salary")
            .values(allowed_roles=["hr", "admin"], acl_version=Document.acl_version + 1)
        )
    with admin_transaction() as s:
        ingest(s, world.docs("itest-a-salary"), EMB, size=60, overlap=10)
        versions = set(s.scalars(select(Chunk.acl_version).where(Chunk.doc_id == "itest-a-salary")))
    assert versions == {2}


def test_ingest_fails_loudly_for_document_without_row_and_writes_nothing(seeded):
    ghost = world.docs("itest-a-leave")[0]
    ghost = type(ghost)(**{**ghost.__dict__, "doc_id": "itest-a-ghost"})
    with pytest.raises(IngestError, match="no row in the documents table"):
        with admin_transaction() as s:
            ingest(s, [*world.docs("itest-a-leave"), ghost], EMB, size=60, overlap=10)
    with admin_transaction() as s:
        assert count_chunks(s, ["itest-a-leave", "itest-a-ghost"]) == 0


def test_ingest_fails_on_tenant_mismatch_with_manifest(seeded):
    doc = world.docs("itest-a-leave")[0]
    wrong = type(doc)(**{**doc.__dict__, "tenant": "itest-b"})
    with pytest.raises(IngestError, match="tenant"), admin_transaction() as s:
        ingest(s, [wrong], EMB, size=60, overlap=10)


# ---------------- verify_index ----------------
def test_verify_index_passes_on_clean_data(ingested):
    with admin_transaction() as s:
        report = verify_index(s)
    assert report.ok, [v for v in report.violations if v.count]
    assert report.total_chunks > 0


def test_verify_index_catches_a_corrupted_acl_copy(ingested):
    """Corrupt one chunk directly as the owner (bypassing ingestion); the audit must notice."""
    with world.scratch() as s:
        victim = s.scalars(select(Chunk.chunk_id).where(Chunk.doc_id == "itest-a-salary")).first()
        # still a *valid* shape (passes every CHECK), but no longer equal to its document's ACL
        s.execute(
            text("UPDATE chunks SET allowed_roles = ARRAY['admin'] WHERE chunk_id = :c"),
            {"c": victim},
        )
        report = verify_index(s)
    bad = {v.name: v for v in report.violations}
    assert not report.ok
    assert bad["chunk_acl_mismatch"].count == 1 and bad["chunk_acl_mismatch"].sample == [
        str(victim)
    ]


def test_verify_index_catches_a_stale_acl_version(ingested):
    with world.scratch() as s:
        s.execute(update(Document).where(Document.id == "itest-a-leave").values(acl_version=7))
        report = verify_index(s)
    assert {v.name: v.count for v in report.violations}["chunk_acl_mismatch"] >= 1


def test_verify_index_cli_exit_codes(ingested, capsys):
    from scripts import verify_index as cli

    assert cli.main() == 0
    assert '"ok": true' in capsys.readouterr().out
