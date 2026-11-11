"""Mutation meta-tests: a leakage suite that cannot fail is worthless.

Each mutant deliberately breaks protections and runs the suite's own check logic (the oracle +
``find_leaks`` + positive controls) on the result. Every mutant that leaves no working layer
MUST make the suite fail. The single-layer runs show the opposite: each layer alone is enough,
which is what defence in depth means.

Runs in-process against the seeded test branch (retrieval only, no LLM), so each mutant takes
seconds.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from sqlalchemy import and_, false, or_, true
from sqlalchemy.orm import Session

from app.auth.policy import EMPLOYEE_ROLES, KNOWN_ROLES, AuthUser
from app.config import get_settings
from app.db.models import Chunk
from app.db.rls import apply_rls_context
from app.db.session import build_engine, transaction
from app.generation.llm import FakeLLM
from app.ingestion.embedder import HashEmbedder
from app.retrieval import authorize as authz
from app.retrieval import service as service_mod
from app.retrieval.acl_filter import build_acl_clause
from app.retrieval.service import RAGService
from tests.security.attacks import (
    canary_targeting,
    cross_tenant_names,
    positive_controls,
    semantic_bait,
)
from tests.security.oracle import Oracle, find_leaks

pytestmark = [pytest.mark.neon, pytest.mark.security]


def _queries(o: Oracle) -> list[str]:
    return [a.question for a in semantic_bait(o) + canary_targeting(o)[::2] + cross_tenant_names(o)]


def run_core(svc: RAGService, o: Oracle, *, personas_per_question: int = 3) -> dict:
    """The suite's verdict on ``svc``: leaks found + positive controls (retrieval level)."""
    active = sorted(k for k, p in o.personas.items() if p.active)
    jobs = [
        (q, active[(i * personas_per_question + j) % len(active)])
        for i, q in enumerate(_queries(o))
        for j in range(personas_per_question)
    ]

    def one(job):  # type: ignore[no-untyped-def]
        q, key = job
        p = o.personas[key]
        chunks, _, _ = svc.retrieve(q, p.user)
        return find_leaks(p, retrieved_docs=[c.doc_id for c in chunks], cited_docs=(),
                          text="\n".join(c.text for c in chunks))  # fmt: skip

    with ThreadPoolExecutor(max_workers=4) as pool:
        leaks = [x for r in pool.map(one, jobs) for x in r]
    controls_ok = []
    for key, q, doc in positive_controls(o):
        chunks, _, _ = svc.retrieve(q, o.personas[key].user)
        controls_ok.append(doc in {c.doc_id for c in chunks})
    import inspect

    verdict = {
        "pairs": len(jobs),
        "leaks": len(leaks),
        "controls_ok": all(controls_ok),
        "passes": not leaks and all(controls_ok),  # the suite's verdict
    }
    print(f"MUTATION {inspect.stack()[1].function}: {verdict}")
    return verdict


# --------------------------------------------------------------------- building blocks
_OWNER = {}


@contextmanager
def owner_transaction():  # type: ignore[no-untyped-def]
    """Owner (BYPASSRLS) session with a pool sized for the 4 worker threads."""
    if "engine" not in _OWNER:
        s = get_settings()
        _OWNER["engine"] = build_engine(s.sqlalchemy_url_direct, pool_size=4, max_overflow=2)
    with Session(_OWNER["engine"]) as session, session.begin():
        yield session


def _svc(**over) -> RAGService:  # type: ignore[no-untyped-def]
    kw = {
        "embedder": HashEmbedder(384),
        "llm": FakeLLM(),
        "top_k": 5,
        "session_factory": transaction,
    }
    return RAGService(**(kw | over))


def _no_tenant_clause(user: AuthUser):  # type: ignore[no-untyped-def]
    """build_acl_clause with the tenant condition deleted (the bug in mutant a)."""
    roles = sorted(user.roles & KNOWN_ROLES)
    return or_(
        Chunk.visibility == "tenant",
        and_(Chunk.visibility == "employees", true() if set(roles) & EMPLOYEE_ROLES else false()),
        and_(Chunk.visibility == "restricted",
             or_(Chunk.allowed_roles.overlap(roles), Chunk.allowed_users.any(user.id))),
    )  # fmt: skip


def _spoofed_identity(o: Oracle):  # type: ignore[no-untyped-def]
    """rls_fn that binds the transaction to ANOTHER tenant's admin (the bug in mutant d)."""

    def rls(session, user):  # type: ignore[no-untyped-def]
        other = next(p.user for p in o.personas.values()
                     if p.user.tenant_id != user.tenant_id and "admin" in p.user.roles)  # fmt: skip
        return apply_rls_context(session, AuthUser(id=other.id, tenant_id=other.tenant_id,
                                                   roles=frozenset(KNOWN_ROLES)))  # fmt: skip

    return rls


@pytest.fixture()
def second_check_disabled(monkeypatch):
    """Replace layer 3 with a pass-through that authorizes every row."""

    def everything(session, user, chunks, **_kw):  # type: ignore[no-untyped-def]
        return [
            authz.AuthorizedChunk(c, user.id, _token=authz._CONSTRUCTION_TOKEN) for c in chunks
        ], []

    monkeypatch.setattr(service_mod, "authorize_chunks", everything)


# --------------------------------------------------------------------- the intact system
def test_intact_system_passes_the_suite(security_world):
    v = run_core(_svc(), security_world)
    assert v["passes"], v


# --------------------------------------------------------------------- mutants: must FAIL
def test_mutant_a_no_tenant_condition_rls_and_check_disabled_fails_the_suite(
    security_world, second_check_disabled
):
    v = run_core(_svc(session_factory=owner_transaction, acl_clause_fn=_no_tenant_clause,
                      rls_fn=lambda s, u: True), security_world)  # fmt: skip
    assert not v["passes"] and v["leaks"] > 0, v


def test_mutant_b_owner_role_bypasses_rls_fails_the_suite(security_world, second_check_disabled):
    """As the table owner (BYPASSRLS) with no WHERE clause and no post-check."""
    v = run_core(_svc(session_factory=owner_transaction, acl_clause_fn=lambda u: true()),
                 security_world)  # fmt: skip
    assert not v["passes"] and v["leaks"] > 0, v


def test_mutant_c_skip_set_config_fails_closed_and_the_suite_still_fails(
    security_world, second_check_disabled
):
    """No RLS context: RLS returns nothing (fail closed) -> no leak, but positive controls fail."""
    v = run_core(_svc(acl_clause_fn=lambda u: true(), rls_fn=lambda s, u: True), security_world)
    assert v["leaks"] == 0 and not v["controls_ok"] and not v["passes"], v


def test_mutant_d_rls_bound_to_wrong_identity_fails_the_suite(
    security_world, second_check_disabled
):
    v = run_core(_svc(acl_clause_fn=lambda u: true(), rls_fn=_spoofed_identity(security_world)),
                 security_world)  # fmt: skip
    assert not v["passes"] and v["leaks"] > 0, v


# --------------------------------------------------------------------- each layer alone suffices
def test_layer1_where_clause_alone_is_enough(security_world, second_check_disabled):
    v = run_core(_svc(session_factory=owner_transaction, acl_clause_fn=build_acl_clause,
                      rls_fn=lambda s, u: True), security_world)  # fmt: skip
    assert v["passes"], v


def test_layer2_rls_alone_is_enough(security_world, second_check_disabled):
    v = run_core(_svc(acl_clause_fn=lambda u: true()), security_world)
    assert v["passes"], v


def test_layer3_second_check_alone_is_enough(security_world):
    """No WHERE, no RLS (owner): forbidden rows reach layer 3, which removes every one of them."""
    v = run_core(_svc(session_factory=owner_transaction, acl_clause_fn=lambda u: true(),
                      rls_fn=lambda s, u: True, top_k=20), security_world)  # fmt: skip
    assert v["leaks"] == 0, v
