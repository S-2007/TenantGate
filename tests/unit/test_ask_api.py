"""/ask with the fake LLM and in-memory retrieval + documents (no DB).

Authentication is covered by test_auth.py; here the caller is injected as an AuthUser. The
retrieval layers (WHERE clause, RLS) are stubbed; the post-retrieval check is the real one.
"""

import uuid
from contextlib import contextmanager

import pytest

from app.api import get_rag_service
from app.auth.dependencies import get_current_user
from app.auth.policy import AuthUser
from app.generation.llm import REFUSAL_TEXT, FakeLLM
from app.generation.prompt import build_user_message, is_refusal, parse_citations
from app.ingestion.embedder import HashEmbedder
from app.retrieval.authorize import AuthorizedChunk, DocumentRecord, authorize_chunks
from app.retrieval.retriever import RetrievedChunk
from app.retrieval.service import RAGService

TENANT = uuid.uuid4()
USER = AuthUser(id=uuid.uuid4(), tenant_id=TENANT, roles=frozenset({"employee"}))


def _chunk(doc_id, title, text, dist, *, tenant=TENANT, cls="internal", roles=()):
    return RetrievedChunk(
        uuid.uuid4(), doc_id, title, 0, text, dist,
        tenant_id=tenant, classification=cls, allowed_roles=tuple(roles), acl_version=1,
    )  # fmt: skip


CHUNKS = [
    _chunk("acme-expense-policy", "Expense Policy", "Meals are reimbursed up to $45 per day.", 0.2),
    _chunk("acme-leave-policy", "Leave Policy", "Full-time employees receive 22 days of paid annual leave.", 0.3),
]  # fmt: skip
DOCS = {c.doc_id: DocumentRecord(c.doc_id, TENANT, c.classification, (), (), 1) for c in CHUNKS}


@contextmanager
def _no_db():
    yield None


def _service(chunks, docs=DOCS):
    return RAGService(
        embedder=HashEmbedder(384),
        llm=FakeLLM(),
        top_k=5,
        session_factory=_no_db,
        search_fn=lambda s, v, k, clause: list(chunks[:k]),
        acl_clause_fn=lambda u: None,  # type: ignore[arg-type,return-value]
        rls_fn=lambda s, u: True,
        doc_loader=lambda s, ids: {i: docs[i] for i in ids if i in docs},
    )


@pytest.fixture(autouse=True)
def _authenticated(client):
    client.app.dependency_overrides[get_current_user] = lambda: USER
    yield
    client.app.dependency_overrides.clear()


def test_ask_returns_cited_answer_and_request_id(client, audit_rows):
    client.app.dependency_overrides[get_rag_service] = lambda: _service(CHUNKS)
    r = client.post(
        "/ask", json={"question": "How many days of paid annual leave do employees get?"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "22 days" in body["answer"]
    assert body["citations"] == [
        {
            "doc_id": "acme-leave-policy",
            "chunk_id": str(CHUNKS[1].chunk_id),
            "title": "Leave Policy",
        }
    ]
    assert body["retrieved_chunk_ids"] == [str(c.chunk_id) for c in CHUNKS]
    assert [x["doc_id"] for x in body["retrieved"]] == [c.doc_id for c in CHUNKS]
    assert r.headers["x-request-id"] == body["request_id"]
    (rec,) = audit_rows
    assert rec.status == "ok" and rec.request_id == body["request_id"]
    assert rec.returned_doc_ids == ["acme-expense-policy", "acme-leave-policy"]


def test_ask_refuses_when_nothing_retrieved(client):
    client.app.dependency_overrides[get_rag_service] = lambda: _service([])
    body = client.post("/ask", json={"question": "anything"}).json()
    assert body["answer"] == REFUSAL_TEXT and body["citations"] == []


def test_ask_validates_input(client, audit_rows):
    assert client.post("/ask", json={"question": ""}).status_code == 422
    assert client.post("/ask", json={}).status_code == 422
    assert [r.status for r in audit_rows] == ["denied", "denied"]


def test_foreign_chunk_never_reaches_response(client, audit_rows):
    foreign = _chunk("other-secret", "Other Tenant Secret", "secret text", 0.1, tenant=uuid.uuid4())
    client.app.dependency_overrides[get_rag_service] = lambda: _service([foreign, *CHUNKS])
    r = client.post("/ask", json={"question": "How many days of paid annual leave?"})
    assert r.status_code == 200
    assert "other-secret" not in r.text and "Other Tenant Secret" not in r.text
    assert "secret text" not in r.text
    assert audit_rows[0].dropped_chunk_ids == [foreign.chunk_id]


def test_parse_citations_ignores_unknown_labels_and_dedups():
    ok, _ = authorize_chunks(None, USER, CHUNKS, doc_loader=lambda s, ids: DOCS)  # type: ignore[arg-type]
    cited = parse_citations("A [C2]. B [C9]. C [C2][C1].", ok)
    assert [c.doc_id for c in cited] == ["acme-leave-policy", "acme-expense-policy"]


def test_prompt_builder_rejects_unverified_rows():
    with pytest.raises(TypeError, match="AuthorizedChunk"):
        build_user_message("q", CHUNKS)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="AuthorizedChunk"):
        parse_citations("[C1]", CHUNKS)  # type: ignore[arg-type]


def test_authorized_chunk_cannot_be_forged_or_mutated():
    with pytest.raises(TypeError):
        AuthorizedChunk(CHUNKS[0], USER.id, _token=object())
    ok, _ = authorize_chunks(None, USER, CHUNKS[:1], doc_loader=lambda s, ids: DOCS)  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        ok[0].user_id = uuid.uuid4()  # type: ignore[misc]
    assert "Meals" not in repr(ok[0])


def test_is_refusal_variants():
    assert is_refusal(REFUSAL_TEXT)
    assert is_refusal("The documents do not contain this information.")
    assert not is_refusal("You get 22 days of leave [C1].")
