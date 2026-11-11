"""Secure retrieval against Neon: WHERE clause, RLS, post-check, and their equivalence.

Covers the Phase 3 Done-when tests: equivalence (Python policy == SQL WHERE == RLS only), narrow
access still gets top-k (iterative HNSW scan), RLS without WHERE, no settings -> nothing,
post-filter vs pre-filter, fault injection (two layers removed at once) and stale ACL.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select, text, true, update
from sqlalchemy.exc import DBAPIError

from app.admin.seed import apply_plan, build_plan
from app.auth.policy import AuthUser, DocACL, can_access
from app.db.models import Chunk, Document, User
from app.db.rls import apply_rls_context
from app.db.session import admin_transaction, transaction
from app.generation.llm import FakeLLM, LLMResponse
from app.ingestion.corpus import load_documents, load_manifest
from app.ingestion.embedder import HashEmbedder
from app.ingestion.pipeline import ingest
from app.metrics import ACL_STALE, ACL_VIOLATIONS
from app.retrieval.acl_filter import build_acl_clause
from app.retrieval.retriever import configure_hnsw, search
from app.retrieval.service import RAGService
from tests.integration import world

pytestmark = pytest.mark.neon
EMB = HashEmbedder(384)


@pytest.fixture(scope="module")
def corpus(app_role):
    """The real 66-document corpus (hash embeddings) seeded + ingested on the test branch."""
    with admin_transaction() as s:
        apply_plan(s, build_plan(load_manifest("data/corpus")), "real-seed-test-pw")
    with admin_transaction() as s:
        ingest(s, load_documents("data/corpus"), EMB, size=500, overlap=50)
    return True


def _all_users() -> list[AuthUser]:
    with admin_transaction() as s:
        return [
            AuthUser(
                id=u.id, tenant_id=u.tenant_id, roles=frozenset(u.roles), is_active=u.is_active
            )
            for u in s.scalars(select(User))
        ]


def _acl(c) -> DocACL:  # type: ignore[no-untyped-def]
    return DocACL(c.doc_id, c.tenant_id, c.classification, frozenset(c.allowed_roles),
                  frozenset(c.allowed_users))  # fmt: skip


def _app_ids(user: AuthUser, *, where: bool) -> set:
    with transaction() as s:
        apply_rls_context(s, user)
        q = select(Chunk.chunk_id)
        if where:
            q = q.where(build_acl_clause(user))
        return set(s.scalars(q))


# ---------------- equivalence ----------------
def test_python_policy_equals_where_clause_equals_rls_only(corpus, ingested):
    with admin_transaction() as s:  # owner: BYPASSRLS, sees everything
        everything = list(s.scalars(select(Chunk)))
        documents = list(s.scalars(select(Document)))
    users = _all_users()
    assert len(users) >= 30 and len(everything) >= 250
    pairs = 0
    for u in users:
        expected = {c.chunk_id for c in everything if can_access(u, _acl(c))}
        assert _app_ids(u, where=True) == expected, f"WHERE differs for {u.id}"
        assert _app_ids(u, where=False) == expected, f"RLS-only differs for {u.id}"
        # documents table RLS mirrors the same rule
        with transaction() as s:
            apply_rls_context(s, u)
            visible_docs = set(s.scalars(select(Document.id)))
        assert visible_docs == {d.id for d in documents if can_access(u, _acl_doc(d))}
        pairs += len(everything)
    assert pairs > 7000  # every (user, chunk) pair was compared


def _acl_doc(d) -> DocACL:  # type: ignore[no-untyped-def]
    return DocACL(d.id, d.tenant_id, d.classification, frozenset(d.allowed_roles),
                  frozenset(d.allowed_users))  # fmt: skip


def test_equivalence_covers_every_rule(corpus, ingested):
    """The seed data really exercises each branch, so the equivalence above is meaningful."""
    users = {(str(u.tenant_id), tuple(sorted(u.roles)), u.is_active) for u in _all_users()}
    roles_seen = {r for _, roles, _ in users for r in roles}
    assert {"contractor", "admin", "hr", "finance", "engineering", "employee"} <= roles_seen
    assert any(not active for *_, active in users)
    with admin_transaction() as s:
        vis = set(s.scalars(select(Chunk.visibility).distinct()))
        user_specific = s.scalar(select(Chunk.chunk_id).where(Chunk.allowed_users != []))
    assert vis == {"tenant", "employees", "restricted"} and user_specific is not None


# ---------------- RLS on its own ----------------
def test_rls_without_settings_returns_nothing(corpus, ingested):
    with transaction() as s:
        assert s.scalar(text("SELECT count(*) FROM chunks")) == 0
        assert s.scalar(text("SELECT count(*) FROM documents")) == 0


def test_rls_settings_do_not_survive_the_transaction(ingested):
    u = AuthUser(
        id=world.uid("itest-a:hr_1"), tenant_id=world.tid("itest-a"), roles=frozenset({"hr"})
    )
    with transaction() as s:
        apply_rls_context(s, u)
        assert s.scalar(text("SELECT count(*) FROM chunks")) > 0
    with transaction() as s:  # possibly the same pooled server connection
        assert s.scalar(text("SELECT count(*) FROM chunks")) == 0


def test_rls_rejects_malformed_identity(ingested):
    with pytest.raises(DBAPIError), transaction() as s:
        s.execute(text("SELECT set_config('app.tenant_id', 'not-a-uuid', true)"))
        s.execute(text("SELECT count(*) FROM chunks")).scalar()


def test_rls_only_query_returns_only_permitted_rows(ingested):
    emp = AuthUser(id=world.uid("itest-a:employee_1"), tenant_id=world.tid("itest-a"),
                   roles=frozenset({"employee"}))  # fmt: skip
    with transaction() as s:
        apply_rls_context(s, emp)
        rows = s.execute(text("SELECT doc_id, tenant_id FROM chunks")).all()
    docs = {r.doc_id for r in rows}
    assert "itest-a-salary" not in docs and "itest-b-leave" not in docs
    assert {"itest-a-leave", "itest-a-review", "itest-a-handbook", "itest-a-careers"} <= docs
    assert {r.tenant_id for r in rows} == {world.tid("itest-a")}


def test_app_role_cannot_turn_rls_off(app_role):
    for stmt in ("ALTER TABLE chunks DISABLE ROW LEVEL SECURITY",
                 "ALTER TABLE chunks NO FORCE ROW LEVEL SECURITY",
                 "DROP POLICY chunks_tenant_acl ON chunks",
                 "SET row_security = off"):  # fmt: skip
        try:
            with transaction() as s:
                s.execute(text(stmt))
                # SET succeeds, but with row_security=off a query that RLS would filter errors
                n = s.scalar(text("SELECT count(*) FROM chunks"))
                assert n == 0
        except DBAPIError as exc:
            msg = str(exc).lower()
            assert "must be owner" in msg or "row-level security" in msg or "permission" in msg


# ---------------- narrow access still gets top-k ----------------
def _narrow_search(user: AuthUser, qvec, *, iterative: str, k: int = 5):  # type: ignore[no-untyped-def]
    with transaction() as s:
        apply_rls_context(s, user)
        configure_hnsw(s, ef_search=10, iterative_scan=iterative)
        # force the HNSW index path (on a table this small the planner might otherwise sort)
        s.execute(text("SET LOCAL enable_seqscan = off"))
        s.execute(text("SET LOCAL enable_bitmapscan = off"))
        s.execute(text("SET LOCAL enable_sort = off"))  # else: tenant btree + sort, no HNSW
        plan = "\n".join(
            r[0]
            for r in s.execute(
                text(
                    "EXPLAIN SELECT chunk_id FROM chunks WHERE tenant_id = :t "
                    "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
                ),
                {"t": user.tenant_id, "q": str(list(qvec)), "k": k},
            )
        )
        rows = search(s, qvec, k, build_acl_clause(user))
    return rows, plan


def test_narrow_access_user_still_gets_top_k_with_iterative_scan(corpus, ingested):
    contractor = AuthUser(id=world.uid("itest-a:contractor_1"), tenant_id=world.tid("itest-a"),
                          roles=frozenset({"contractor"}))  # fmt: skip
    q = EMB.embed_query("deploy the zephyr pipeline after the canary soak passes")  # handbook-like
    off, plan = _narrow_search(contractor, q, iterative="off")
    on, _ = _narrow_search(contractor, q, iterative="strict_order")
    assert "ix_chunks_embedding_hnsw" in plan  # the test really exercises the HNSW path
    assert len(off) < 5  # the pgvector caveat, reproduced
    assert len(on) == 5  # ...and fixed
    assert {r.doc_id for r in on} == {"itest-a-careers"}  # the only doc a contractor may read
    dists = [r.distance for r in on]
    # strict_order keeps distance order (index uses float4; allow rounding noise)
    assert all(a <= b + 1e-6 for a, b in zip(dists, dists[1:], strict=False))


# ---------------- post-filter vs pre-filter (interview material) ----------------
def test_post_filtering_returns_nothing_where_pre_filtering_returns_best_allowed_5(
    corpus, ingested
):
    emp = AuthUser(id=world.uid("itest-a:employee_1"), tenant_id=world.tid("itest-a"),
                   roles=frozenset({"employee"}))  # fmt: skip
    q = EMB.embed_query("itest widget calibration interval hours section")  # salary-band text
    with admin_transaction() as s:  # retrieve-then-drop: unfiltered top-10, then policy
        top10 = search(s, q, 10, true())
    assert all(not can_access(emp, _acl(c)) for c in top10)  # all forbidden
    post = [c for c in top10 if can_access(emp, _acl(c))][:5]
    with transaction() as s:  # pre-filter: ACL inside the vector query (+ RLS)
        apply_rls_context(s, emp)
        configure_hnsw(s, ef_search=100, iterative_scan="strict_order")
        pre = search(s, q, 5, build_acl_clause(emp))
    assert post == []
    assert len(pre) == 5 and all(can_access(emp, _acl(c)) for c in pre)
    assert {c.doc_id for c in pre} <= {"itest-a-leave", "itest-a-review", "itest-a-handbook",
                                       "itest-a-careers"}  # fmt: skip


# ---------------- defence in depth ----------------
class RecordingLLM:
    """Fake LLM that remembers every prompt it was given."""

    provider, model = "fake", "recording"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self._inner = FakeLLM()

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> LLMResponse:
        self.prompts.append(user)
        return self._inner.complete(system, user, max_tokens=max_tokens)


def test_fault_injection_where_and_rls_both_off_second_check_still_holds(ingested, caplog):
    """Simulate two bugs at once: no WHERE clause AND no RLS (owner session, no context)."""
    emp = AuthUser(id=world.uid("itest-a:employee_1"), tenant_id=world.tid("itest-a"),
                   roles=frozenset({"employee"}))  # fmt: skip
    llm = RecordingLLM()
    svc = RAGService(
        embedder=EMB,
        llm=llm,
        top_k=12,
        session_factory=admin_transaction,  # BYPASSRLS = RLS effectively disabled
        acl_clause_fn=lambda u: true(),  # WHERE clause removed
        rls_fn=lambda s, u: True,  # no RLS context
    )
    before = ACL_VIOLATIONS.total()
    with caplog.at_level(logging.WARNING, logger="app.security"):
        r = svc.ask("Itest A staff annual leave sick days and widget calibration interval", emp)
    forbidden = [d for d in r.dropped if d.reason != "stale_acl"]
    assert forbidden, "the broken layers must have let forbidden rows through to layer 3"
    with admin_transaction() as s:
        acls = {d.id: _acl_doc(d) for d in s.scalars(select(Document))}
    assert all(not can_access(emp, acls[d.doc_id]) for d in forbidden)  # really forbidden
    assert all(can_access(emp, acls[c.doc_id]) for c in r.retrieved)  # survivors allowed
    sent = "\n".join(llm.prompts)
    for d in forbidden:  # nothing of a forbidden document reached the model
        assert f"doc_id={d.doc_id}," not in sent
    assert "$123,456" not in sent and "Itest B staff" not in sent
    assert "SECURITY_ALERT" in caplog.text
    assert ACL_VIOLATIONS.total() >= before + len(forbidden)


def test_stale_acl_chunk_is_dropped_and_counted(ingested, caplog):
    emp = AuthUser(id=world.uid("itest-a:employee_1"), tenant_id=world.tid("itest-a"),
                   roles=frozenset({"employee"}))  # fmt: skip
    with admin_transaction() as s:  # ACL changed in documents, chunks not re-synced yet
        s.execute(
            update(Document).where(Document.id == "itest-a-leave")
            .values(acl_version=Document.acl_version + 1)
        )  # fmt: skip
    llm = RecordingLLM()
    svc = RAGService(embedder=EMB, llm=llm, top_k=5, session_factory=transaction)
    before = ACL_STALE.value()
    with caplog.at_level(logging.WARNING, logger="app.security"):
        r = svc.ask("How many sick days do Itest A staff receive per year?", emp)
    stale = [d for d in r.dropped if d.reason == "stale_acl"]
    assert [d.doc_id for d in stale] == ["itest-a-leave"]
    assert ACL_STALE.value() == before + 1 and "ACL_RESYNC_NEEDED" in caplog.text
    assert "31 days" not in "\n".join(llm.prompts)
    # re-sync fixes it
    with admin_transaction() as s:
        ingest(s, world.docs("itest-a-leave"), EMB, size=60, overlap=10)
    r2 = svc.ask("How many sick days do Itest A staff receive per year?", emp)
    assert "itest-a-leave" in {c.doc_id for c in r2.retrieved} and not r2.dropped
