"""Idempotent, ACL-aware ingestion: ACL lookup -> chunk -> embed -> validate -> batched upsert.

- The ACL of every chunk comes from the ``documents`` table (the source of truth), never from
  the file or the manifest. A document without a ``documents`` row is an error: ingestion raises
  ``IngestError`` before embedding anything, and the CLI exits non-zero. Nothing is skipped.
- Every row passes ``ChunkPayload`` validation before the first write. One bad chunk aborts the
  whole run, and the caller's transaction rolls back.
- Idempotency: deterministic chunk ids (uuid5 of doc_id + chunk_index) and
  ``INSERT ... ON CONFLICT (chunk_id) DO UPDATE``. Re-running unchanged input rewrites the same
  rows. A document that shrank has its trailing stale chunks deleted. ``prune=True`` also deletes
  chunks of documents not in the input set.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import bindparam, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import Chunk, Document, Tenant
from app.ingestion.chunker import TextChunk, chunk_text, embedding_input
from app.ingestion.corpus import DocumentRecord
from app.ingestion.embedder import Embedder
from app.ingestion.payload import ChunkPayload

logger = logging.getLogger(__name__)

UPSERT_BATCH = 64


class IngestError(RuntimeError):
    """Ingestion refused to write (missing ACL, tenant mismatch, invalid chunk)."""


@dataclass(frozen=True)
class StoredACL:
    """A document's ACL as stored in Postgres."""

    doc_id: str
    tenant_id: uuid.UUID
    tenant_slug: str
    classification: str
    allowed_roles: tuple[str, ...]
    allowed_users: tuple[uuid.UUID, ...]
    acl_version: int


@dataclass
class IngestStats:
    """Summary of one ingest run."""

    documents: int = 0
    chunks: int = 0
    inserted: int = 0
    updated: int = 0
    stale_deleted: int = 0
    pruned: int = 0
    embed_seconds: float = 0.0
    write_seconds: float = 0.0


def build_chunks(
    docs: Sequence[DocumentRecord], embedder: Embedder, *, size: int, overlap: int
) -> list[TextChunk]:
    """Chunk every document with the embedder's tokenizer."""
    if size + 2 > embedder.max_tokens:
        logger.warning(
            "CHUNK_SIZE=%d exceeds %s max input (%d tokens incl. specials); tails will be truncated",
            size,
            embedder.name,
            embedder.max_tokens,
        )
    out: list[TextChunk] = []
    for d in docs:
        out.extend(
            chunk_text(
                d.text,
                doc_id=d.doc_id,
                title=d.title,
                tokenizer=embedder.tokenizer,
                size=size,
                overlap=overlap,
            )
        )
    return out


def load_acls(session: Session, docs: Sequence[DocumentRecord]) -> dict[str, StoredACL]:
    """ACL of every input document from the DB. Raises IngestError if any is missing/mismatched."""
    ids = [d.doc_id for d in docs]
    rows = session.execute(
        select(Document, Tenant.slug)
        .join(Tenant, Tenant.id == Document.tenant_id)
        .where(Document.id.in_(ids))
    ).all()
    acls = {
        doc.id: StoredACL(
            doc_id=doc.id,
            tenant_id=doc.tenant_id,
            tenant_slug=slug,
            classification=doc.classification,
            allowed_roles=tuple(sorted(doc.allowed_roles)),
            allowed_users=tuple(sorted(doc.allowed_users, key=str)),
            acl_version=doc.acl_version,
        )
        for doc, slug in rows
    }
    missing = sorted(set(ids) - set(acls))
    if missing:
        raise IngestError(
            f"{len(missing)} document(s) have no row in the documents table (run "
            f"`python -m scripts.seed` first): {', '.join(missing[:10])}"
        )
    wrong = sorted(d.doc_id for d in docs if acls[d.doc_id].tenant_slug != d.tenant)
    if wrong:
        raise IngestError(f"tenant in manifest differs from documents table for: {wrong[:10]}")
    return acls


def build_payloads(
    chunks: Sequence[TextChunk],
    vectors: Sequence[Sequence[float]],
    acls: dict[str, StoredACL],
) -> list[ChunkPayload]:
    """Validate every row before any write; one invalid chunk raises IngestError."""
    if len(chunks) != len(vectors):
        raise IngestError(f"{len(chunks)} chunks but {len(vectors)} embeddings")
    out = []
    for c, v in zip(chunks, vectors, strict=True):
        acl = acls[c.doc_id]
        try:
            out.append(
                ChunkPayload(
                    chunk_id=c.chunk_id,
                    doc_id=c.doc_id,
                    tenant_id=acl.tenant_id,
                    title=c.title,
                    chunk_index=c.chunk_index,
                    text=c.text,
                    embedding=tuple(float(x) for x in v),
                    classification=acl.classification,
                    allowed_roles=acl.allowed_roles,
                    allowed_users=acl.allowed_users,
                    acl_version=acl.acl_version,
                )
            )
        except ValidationError as exc:
            raise IngestError(
                f"invalid chunk {c.doc_id}#{c.chunk_index}: "
                + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            ) from exc
    return out


def ingest(
    session: Session,
    docs: Sequence[DocumentRecord],
    embedder: Embedder,
    *,
    size: int,
    overlap: int,
    prune: bool = False,
) -> IngestStats:
    """ACL lookup, chunk, embed, validate and upsert ``docs`` in the caller's transaction."""
    stats = IngestStats(documents=len(docs))
    acls = load_acls(session, docs)  # fail before spending CPU on embeddings
    chunks = build_chunks(docs, embedder, size=size, overlap=overlap)
    stats.chunks = len(chunks)

    t0 = time.perf_counter()
    vectors = embedder.embed_documents([embedding_input(c) for c in chunks])
    stats.embed_seconds = round(time.perf_counter() - t0, 2)
    payloads = build_payloads(chunks, vectors, acls)
    return write_chunks(session, docs, payloads, prune=prune, stats=stats)


_UPDATABLE = (
    "doc_id",
    "tenant_id",
    "title",
    "chunk_index",
    "text",
    "embedding",
    "classification",
    "allowed_roles",
    "allowed_users",
    "acl_version",
)


def write_chunks(
    session: Session,
    docs: Sequence[DocumentRecord],
    payloads: Sequence[ChunkPayload],
    *,
    prune: bool,
    stats: IngestStats,
) -> IngestStats:
    """Batched upsert + stale-chunk cleanup inside the caller's transaction."""
    t0 = time.perf_counter()
    rows = [p.row() for p in payloads]
    for i in range(0, len(rows), UPSERT_BATCH):
        stmt = insert(Chunk).values(rows[i : i + UPSERT_BATCH])
        stmt = stmt.on_conflict_do_update(
            index_elements=[Chunk.chunk_id],
            set_={col: stmt.excluded[col] for col in _UPDATABLE},
        ).returning(text("(xmax = 0) AS inserted"))
        for (was_insert,) in session.execute(stmt):
            if was_insert:
                stats.inserted += 1
            else:
                stats.updated += 1

    # Trailing chunks of documents that now produce fewer chunks.
    counts: dict[str, int] = {}
    for p in payloads:
        counts[p.doc_id] = counts.get(p.doc_id, 0) + 1
    for d in docs:
        res = session.execute(
            delete(Chunk).where(
                Chunk.doc_id == d.doc_id, Chunk.chunk_index >= counts.get(d.doc_id, 0)
            )
        )
        stats.stale_deleted += res.rowcount or 0

    if prune:
        keep = [d.doc_id for d in docs]
        res = session.execute(
            delete(Chunk).where(Chunk.doc_id.not_in(bindparam("keep", expanding=True))),
            {"keep": keep},
        )
        stats.pruned = res.rowcount or 0
    stats.write_seconds = round(time.perf_counter() - t0, 2)
    return stats


def count_chunks(session: Session, doc_ids: Sequence[str] | None = None) -> int:
    """Number of stored chunks (optionally restricted to ``doc_ids``)."""
    q = select(func.count()).select_from(Chunk)
    if doc_ids is not None:
        q = q.where(Chunk.doc_id.in_(list(doc_ids)))
    return int(session.execute(q).scalar_one())
