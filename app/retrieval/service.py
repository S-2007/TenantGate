"""RAG service layer: secure retrieve -> verify -> prompt -> generate -> parse citations.

The HTTP API and the eval harness both call this, so eval numbers measure the code path users hit.

One request, three independent access-control layers, all in ONE database transaction:
  1. the ACL predicate inside the vector query (``build_acl_clause``),
  2. Postgres RLS, bound to the caller by ``apply_rls_context`` (set_config(..., true)),
  3. ``authorize_chunks``: can_access on every row + the authoritative documents row.
Only the ``AuthorizedChunk`` output of layer 3 can reach the prompt. The transaction is closed
before the LLM call, so no connection is held during generation.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field

from sqlalchemy import ColumnElement
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.auth.policy import AuthUser
from app.db.rls import apply_rls_context
from app.generation.llm import REFUSAL_TEXT, LLMClient
from app.generation.prompt import SYSTEM_PROMPT, build_user_message, is_refusal, parse_citations
from app.ingestion.embedder import Embedder
from app.retrieval.acl_filter import build_acl_clause
from app.retrieval.authorize import (
    AuthorizedChunk,
    DocLoader,
    DroppedChunk,
    authorize_chunks,
    load_document_acls,
)
from app.retrieval.retriever import RetrievedChunk, configure_hnsw, search

logger = logging.getLogger(__name__)

SearchFn = Callable[[Session, Sequence[float], int, ColumnElement[bool]], list[RetrievedChunk]]
AclClauseFn = Callable[[AuthUser], ColumnElement[bool]]
RlsFn = Callable[[Session, AuthUser], bool]


@dataclass
class AskResult:
    """Full result of one question (the API exposes a subset)."""

    question: str
    answer: str
    refused: bool
    retrieved: list[AuthorizedChunk]
    cited: list[AuthorizedChunk]
    dropped: list[DroppedChunk] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)
    llm_model: str = ""


class RAGService:
    """Stateless orchestrator; every dependency is injectable (tests swap layers to fault-inject)."""

    def __init__(
        self,
        *,
        embedder: Embedder,
        llm: LLMClient,
        top_k: int,
        session_factory: Callable[[], AbstractContextManager[Session]],
        search_fn: SearchFn = search,
        acl_clause_fn: AclClauseFn = build_acl_clause,
        rls_fn: RlsFn = apply_rls_context,
        doc_loader: DocLoader = load_document_acls,
        hnsw_ef_search: int = 100,
        hnsw_iterative_scan: str = "strict_order",
    ) -> None:
        self.embedder = embedder
        self.llm = llm
        self.top_k = top_k
        self.session_factory = session_factory
        self.search_fn = search_fn
        self.acl_clause_fn = acl_clause_fn
        self.rls_fn = rls_fn
        self.doc_loader = doc_loader
        self.hnsw_ef_search = hnsw_ef_search
        self.hnsw_iterative_scan = hnsw_iterative_scan

    def retrieve(
        self, question: str, user: AuthUser, *, request_id: str = "-"
    ) -> tuple[list[AuthorizedChunk], list[DroppedChunk], dict[str, float]]:
        """Embed, then (one transaction) bind RLS, pre-filtered search, post-check."""
        t0 = time.perf_counter()
        qvec = self.embedder.embed_query(question)
        t1 = time.perf_counter()
        # The whole read is one read-only transaction, so a dropped connection (Neon pooler
        # restart, network blip) is retried once from scratch. A second failure propagates.
        for attempt in (1, 2):
            try:
                with self.session_factory() as s:
                    if s is not None:
                        self.rls_fn(s, user)
                        configure_hnsw(
                            s,
                            ef_search=self.hnsw_ef_search,
                            iterative_scan=self.hnsw_iterative_scan,
                        )
                    rows = self.search_fn(s, qvec, self.top_k, self.acl_clause_fn(user))
                    t2 = time.perf_counter()
                    authorized, dropped = authorize_chunks(
                        s, user, rows, request_id=request_id, doc_loader=self.doc_loader
                    )
                break
            except OperationalError:
                if attempt == 2:
                    raise
                logger.warning("retrieval transaction lost its connection; retrying once")
        t3 = time.perf_counter()
        return (
            authorized,
            dropped,
            {
                "embed": (t1 - t0) * 1000,
                "search": (t2 - t1) * 1000,
                "verify": (t3 - t2) * 1000,
            },
        )

    def ask(self, question: str, user: AuthUser, *, request_id: str = "-") -> AskResult:
        """Answer ``question`` for ``user`` from authorized chunks only.

        Per-user chat history would be loaded here (scoped by user.id AND user.tenant_id, behind
        the same RLS context) - deliberately not built.
        """
        chunks, dropped, timings = self.retrieve(question, user, request_id=request_id)
        t0 = time.perf_counter()
        if not chunks:
            answer, model = REFUSAL_TEXT, ""
        else:
            resp = self.llm.complete(SYSTEM_PROMPT, build_user_message(question, chunks))
            answer, model = resp.text, resp.model
        timings["generate"] = (time.perf_counter() - t0) * 1000
        timings["total"] = sum(timings.values())
        refused = is_refusal(answer)
        cited = [] if refused else parse_citations(answer, chunks)
        return AskResult(
            question=question,
            answer=answer,
            refused=refused,
            retrieved=chunks,
            cited=cited,
            dropped=dropped,
            timings_ms={k: round(v, 1) for k, v in timings.items()},
            llm_model=model,
        )
