"""ACL-pre-filtered top-k cosine retrieval with pgvector (one statement: WHERE <acl> ORDER BY <=>).

The ACL predicate and the nearest-neighbour ordering are in the SAME statement, so the index scan
itself only yields permitted rows; nothing is retrieved and then dropped.

pgvector caveat: an HNSW scan explores only ``hnsw.ef_search`` candidates, then the WHERE clause
filters them, so a selective filter can return FEWER than k rows. With
``hnsw.iterative_scan = strict_order`` (pgvector >= 0.8) the scan keeps going until k permitted
rows are found (or ``hnsw.max_scan_tuples`` is hit), preserving exact distance order. Both are
set transaction-locally with ``set_config(..., true)`` so they never leak across pooled
connections. See docs/DECISIONS.md D-025.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import ColumnElement, select, text
from sqlalchemy.orm import Session

from app.db.models import Chunk


@dataclass(frozen=True)
class RetrievedChunk:
    """A chunk returned by vector search: payload for citations + its ACL copy for re-checking."""

    chunk_id: uuid.UUID
    doc_id: str
    title: str
    chunk_index: int
    text: str
    distance: float  # cosine distance, 0 = identical
    tenant_id: uuid.UUID | None = None
    classification: str = ""
    allowed_roles: tuple[str, ...] = field(default_factory=tuple)
    allowed_users: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    acl_version: int = 0

    @property
    def score(self) -> float:
        """Cosine similarity (1 - distance)."""
        return 1.0 - self.distance


def configure_hnsw(session: Session, *, ef_search: int, iterative_scan: str) -> None:
    """Transaction-local HNSW settings (bound values; never SET without LOCAL)."""
    session.execute(
        text(
            "SELECT set_config('hnsw.ef_search', :ef, true), "
            "set_config('hnsw.iterative_scan', :mode, true)"
        ),
        {"ef": str(int(ef_search)), "mode": iterative_scan},
    )


def search(
    session: Session,
    query_vec: Sequence[float],
    k: int,
    acl_clause: ColumnElement[bool],
) -> list[RetrievedChunk]:
    """``SELECT ... WHERE <acl_clause> ORDER BY embedding <=> :q LIMIT :k``."""
    dist = Chunk.embedding.cosine_distance(list(query_vec)).label("distance")
    stmt = (
        select(
            Chunk.chunk_id,
            Chunk.doc_id,
            Chunk.title,
            Chunk.chunk_index,
            Chunk.text,
            dist,
            Chunk.tenant_id,
            Chunk.classification,
            Chunk.allowed_roles,
            Chunk.allowed_users,
            Chunk.acl_version,
        )
        .where(acl_clause)
        .order_by(dist)
        .limit(k)
    )
    return [
        RetrievedChunk(
            chunk_id=r.chunk_id,
            doc_id=r.doc_id,
            title=r.title,
            chunk_index=r.chunk_index,
            text=r.text,
            distance=float(r.distance),
            tenant_id=r.tenant_id,
            classification=r.classification,
            allowed_roles=tuple(r.allowed_roles),
            allowed_users=tuple(r.allowed_users),
            acl_version=r.acl_version,
        )
        for r in session.execute(stmt)
    ]
