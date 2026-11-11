"""Pure tests for Phase 3 building blocks (no database)."""

from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.audit.log import query_hash, status_for
from app.auth.policy import AuthUser
from app.metrics import ACL_VIOLATIONS, Counter, render_all
from app.request_context import new_request_id
from app.retrieval.acl_filter import build_acl_clause
from app.retrieval.authorize import DocumentRecord, authorize_chunks
from app.retrieval.retriever import RetrievedChunk

T = uuid.uuid4()
U = uuid.uuid4()


def _compile(clause):  # type: ignore[no-untyped-def]
    return clause.compile(dialect=postgresql.dialect())


def test_acl_clause_binds_every_value_and_never_inlines_it():
    user = AuthUser(id=U, tenant_id=T, roles=frozenset({"hr", "employee", "evil'); --"}))
    c = _compile(build_acl_clause(user))
    sql = str(c)
    assert str(T) not in sql and str(U) not in sql and "hr" not in sql and "evil" not in sql
    assert c.params["acl_tenant"] == T and c.params["acl_user_id"] == U
    assert c.params["acl_roles"] == ["employee", "hr"]  # unknown role dropped
    assert c.params["acl_is_employee"] is True
    assert "chunks.tenant_id = %(acl_tenant)s" in sql
    assert "chunks.allowed_roles && %(acl_roles)s" in sql


def test_acl_clause_non_employee_and_denials():
    c = _compile(build_acl_clause(AuthUser(id=U, tenant_id=T, roles=frozenset({"contractor"}))))
    assert c.params["acl_is_employee"] is False
    for user in (None, AuthUser(id=U, tenant_id=T, is_active=False)):
        assert str(_compile(build_acl_clause(user))) == "false"
    bad = AuthUser(id=U, tenant_id="nope", roles=frozenset({"admin"}))  # type: ignore[arg-type]
    assert str(_compile(build_acl_clause(bad))) == "false"


def _row(**over):  # type: ignore[no-untyped-def]
    base = dict(
        chunk_id=uuid.uuid4(), doc_id="d1", title="t", chunk_index=0, text="secret body",
        distance=0.1, tenant_id=T, classification="confidential", allowed_roles=("hr",),
        allowed_users=(), acl_version=3,
    )  # fmt: skip
    return RetrievedChunk(**(base | over))


DOC = DocumentRecord("d1", T, "confidential", ("hr",), (), 3)
HR = AuthUser(id=U, tenant_id=T, roles=frozenset({"hr"}))


@pytest.mark.parametrize(
    "row,doc,reason",
    [
        (_row(allowed_roles=("finance",)), DOC, "policy_denied"),
        (_row(), None, "document_missing"),
        (
            _row(),
            DocumentRecord("d1", uuid.uuid4(), "confidential", ("hr",), (), 3),
            "tenant_mismatch",
        ),
        (_row(acl_version=2), DOC, "stale_acl"),
        (
            _row(),
            DocumentRecord("d1", T, "confidential", ("finance",), (), 3),
            "document_policy_denied",
        ),
    ],
)
def test_second_check_reasons(row, doc, reason, caplog):
    loader = lambda s, ids: ({"d1": doc} if doc else {})  # noqa: E731
    before = ACL_VIOLATIONS.value(reason)
    with caplog.at_level(logging.WARNING, logger="app.security"):
        ok, dropped = authorize_chunks(None, HR, [row], doc_loader=loader, request_id="r1")  # type: ignore[arg-type]
    assert ok == [] and [d.reason for d in dropped] == [reason]
    tag = "ACL_RESYNC_NEEDED" if reason == "stale_acl" else "SECURITY_ALERT"
    assert (
        tag in caplog.text and '"chunk_id"' in caplog.text and '"request_id": "r1"' in caplog.text
    )
    assert "secret body" not in caplog.text  # alerts carry ids, never text
    if reason != "stale_acl":
        assert ACL_VIOLATIONS.value(reason) == before + 1


def test_second_check_passes_valid_row():
    ok, dropped = authorize_chunks(None, HR, [_row()], doc_loader=lambda s, ids: {"d1": DOC})  # type: ignore[arg-type]
    assert len(ok) == 1 and dropped == [] and ok[0].user_id == U


def test_metrics_text_format():
    c = Counter("x_total", "help", label="reason", initial_labels=("a",))
    c.inc("b", 2)
    assert c.render() == ["# HELP x_total help", "# TYPE x_total counter",
                          'x_total{reason="a"} 0', 'x_total{reason="b"} 2']  # fmt: skip
    with pytest.raises(ValueError):
        c.inc("a", -1)
    text = render_all()
    assert "# TYPE acl_violations_total counter" in text
    assert 'acl_violations_total{reason="policy_denied"}' in text


def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "acl_violations_total" in r.text and "audit_write_failures_total" in r.text


def test_request_id_accepts_only_uuids():
    good = str(uuid.uuid4())
    assert new_request_id(good) == good
    for bad in (None, "", "hello\nINJECTED log line", "1; drop"):
        rid = new_request_id(bad)
        assert uuid.UUID(rid) and rid != bad


def test_request_id_header_on_every_response(client):
    rid = str(uuid.uuid4())
    assert client.get("/metrics", headers={"X-Request-ID": rid}).headers["x-request-id"] == rid
    assert uuid.UUID(client.get("/metrics").headers["x-request-id"])


def test_query_hash_is_keyed_and_normalised():
    a = query_hash("  How many PTO days?  ", "k1" * 20)
    assert a == query_hash("how   many pto DAYS?", "k1" * 20)
    assert a != query_hash("how many pto days?", "k2" * 20)  # keyed: not a plain sha256
    assert len(a) == 64 and int(a, 16) >= 0


@pytest.mark.parametrize("code,status", [(200, "ok"), (401, "denied"), (422, "denied"),
                                         (404, "denied"), (502, "error"), (500, "error")])  # fmt: skip
def test_audit_status_mapping(code, status):
    assert status_for(code) == status


def test_audit_failure_withholds_the_answer(client, audit_rows):
    from app.auth.dependencies import get_current_user

    def broken(_rec):  # type: ignore[no-untyped-def]
        raise RuntimeError("db down")

    client.app.state.audit_writer = broken
    client.app.dependency_overrides[get_current_user] = lambda: HR
    r = client.post("/ask", json={"question": "anything", "tenant_id": "x"})
    assert r.status_code == 503 and "audit unavailable" in r.text
    client.app.dependency_overrides.clear()


def test_retrieval_retries_once_on_a_dropped_connection_then_gives_up():
    from contextlib import contextmanager

    from sqlalchemy.exc import OperationalError

    from app.generation.llm import FakeLLM
    from app.ingestion.embedder import HashEmbedder
    from app.retrieval.service import RAGService

    calls = {"n": 0}

    def make(fail_times: int):  # type: ignore[no-untyped-def]
        @contextmanager
        def factory():  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] <= fail_times:
                raise OperationalError("SELECT 1", {}, Exception("server closed the connection"))
            yield None

        return factory

    def svc(factory):  # type: ignore[no-untyped-def]
        return RAGService(
            embedder=HashEmbedder(384), llm=FakeLLM(), top_k=5, session_factory=factory,
            search_fn=lambda s, v, k, c: [], acl_clause_fn=lambda u: None,  # type: ignore[arg-type,return-value]
            rls_fn=lambda s, u: True, doc_loader=lambda s, ids: {},
        )  # fmt: skip

    ok, dropped, _ = svc(make(1)).retrieve("q", HR)
    assert calls["n"] == 2 and ok == [] and dropped == []
    calls["n"] = 0
    with pytest.raises(OperationalError):
        svc(make(5)).retrieve("q", HR)
    assert calls["n"] == 2  # exactly one retry
