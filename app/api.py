"""POST /ask: authenticated, ACL-filtered retrieval-augmented answers.

- Identity comes only from the verified token + DB (``get_current_user``). The body may carry
  nothing but the question (extra fields -> 422, logged as suspicious).
- Retrieval is protected three ways (WHERE clause, RLS, post-check); see app/retrieval/service.py.
- The response exposes only authorized chunks: their ids, doc ids, titles and scores. It never
  carries chunk text, dropped chunks, SQL or provider error details.
- The audit row is filled here and written by the request middleware in app/main.py, so a
  401/422 that never reaches this function is audited too.
"""

from __future__ import annotations

import time
import uuid
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import OperationalError

from app.audit.log import AuditRecord
from app.auth.dependencies import get_current_user
from app.auth.policy import AuthUser
from app.config import get_settings
from app.db.session import transaction
from app.generation.llm import LLMError, get_chain
from app.ingestion.embedder import get_embedder
from app.retrieval.service import RAGService

router = APIRouter()


class AskRequest(BaseModel):
    """Question payload. Only ``question`` is accepted (extra fields -> 422)."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)


class Citation(BaseModel):
    """A source the answer cites (always one of the caller's authorized chunks)."""

    doc_id: str
    chunk_id: uuid.UUID
    title: str


class RetrievedRef(BaseModel):
    """An authorized chunk that was given to the model as context."""

    chunk_id: uuid.UUID
    doc_id: str
    score: float


class AskResponse(BaseModel):
    """Answer plus provenance."""

    answer: str
    citations: list[Citation]
    retrieved_chunk_ids: list[uuid.UUID]
    retrieved: list[RetrievedRef]
    model: str  # which model answered ("" when nothing was retrieved); provenance only
    request_id: str


@lru_cache(maxsize=1)
def get_rag_service() -> RAGService:
    """Process-wide service built from settings (override in tests via dependency_overrides)."""
    s = get_settings()
    return RAGService(
        embedder=get_embedder(s.embedding_model, s.embedding_dim),
        llm=get_chain(s.answer_specs(), s.llm_keys()),
        top_k=s.top_k,
        session_factory=transaction,
        hnsw_ef_search=s.hnsw_ef_search,
        hnsw_iterative_scan=s.hnsw_iterative_scan,
    )


@router.post("/ask", response_model=AskResponse)
def ask(
    req: AskRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),  # noqa: B008
    svc: RAGService = Depends(get_rag_service),  # noqa: B008
) -> AskResponse:
    """Retrieve the caller's permitted top-k chunks and answer only from them, with citations."""
    request_id = request.state.request_id
    rec = AuditRecord(request_id=request_id, user=user, question=req.question)
    request.state.audit = rec
    t0 = time.perf_counter()
    try:
        result = svc.ask(req.question, user, request_id=request_id)
    except LLMError as exc:
        rec.status = "error"
        raise HTTPException(status_code=502, detail="LLM provider error") from exc
    except (ConnectionError, OperationalError) as exc:
        rec.status = "error"
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    finally:
        rec.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    rec.returned_chunk_ids = [c.chunk_id for c in result.retrieved]
    rec.returned_doc_ids = [c.doc_id for c in result.retrieved]
    rec.dropped_chunk_ids = [d.chunk_id for d in result.dropped]
    rec.model = result.llm_model or None
    rec.status = "ok"
    return AskResponse(
        answer=result.answer,
        citations=[
            Citation(doc_id=c.doc_id, chunk_id=c.chunk_id, title=c.title) for c in result.cited
        ],
        retrieved_chunk_ids=[c.chunk_id for c in result.retrieved],
        retrieved=[
            RetrievedRef(chunk_id=c.chunk_id, doc_id=c.doc_id, score=round(c.score, 6))
            for c in result.retrieved
        ],
        model=result.llm_model,
        request_id=request_id,
    )


class DocumentSummary(BaseModel):
    """A document the caller may read (no content, no ACL details)."""

    doc_id: str
    title: str
    classification: str


@router.get("/documents", response_model=list[DocumentSummary])
def my_documents(user: AuthUser = Depends(get_current_user)) -> list[DocumentSummary]:  # noqa: B008
    """Titles of every document the caller may read.

    Filtered three times, like retrieval: explicit tenant WHERE, RLS on ``documents`` (bound to
    the caller in this transaction), then ``can_access`` on each row.
    """
    from sqlalchemy import select

    from app.auth.policy import DocACL, can_access
    from app.db.models import Document
    from app.db.rls import apply_rls_context

    with transaction() as s:
        if not apply_rls_context(s, user):
            return []
        rows = s.execute(
            select(
                Document.id,
                Document.title,
                Document.classification,
                Document.tenant_id,
                Document.allowed_roles,
                Document.allowed_users,
            )
            # Phase 6: uploads still processing / failed are listed in /documents/mine only
            .where(Document.tenant_id == user.tenant_id, Document.status == "ready")
            .order_by(Document.classification, Document.title)
        ).all()
    return [
        DocumentSummary(doc_id=r.id, title=r.title, classification=r.classification)
        for r in rows
        if can_access(
            user,
            DocACL(
                r.id,
                r.tenant_id,
                r.classification,
                frozenset(r.allowed_roles),
                frozenset(r.allowed_users),
            ),
        )  # fmt: skip
    ]
