"""Post-retrieval re-verification (layer 3 of 3) and the ``AuthorizedChunk`` type.

Layers 1 (the WHERE clause) and 2 (RLS) should already have removed every forbidden row. This
layer assumes both could be wrong. For every retrieved chunk, in the same transaction:

1. ``can_access(user, <the chunk's own ACL columns>)``; fail -> ``policy_denied``
2. the authoritative ``documents`` row (read under the same RLS context) must exist ->
   ``document_missing``, must have the chunk's tenant -> ``tenant_mismatch``, must have the same
   ``acl_version`` -> ``stale_acl`` (the chunk copy is out of date), and ``can_access`` on the
   document's own ACL must allow it -> ``document_policy_denied``, and the document must be
   ``ready`` (not processing / failed) -> ``document_not_ready`` (Phase 6)

Failures are dropped, never "fixed". Every reason except ``stale_acl`` is a SECURITY ALERT (one
structured log line per chunk) and increments ``acl_violations_total{reason}``. ``stale_acl`` is
a data-freshness problem, not an attack: it is logged as a re-sync note and counted in
``acl_stale_chunks_total``. The request still answers from the chunks that passed.

Structural guarantee: ``AuthorizedChunk`` can only be constructed here (a private token is
required), and the prompt builder only accepts ``AuthorizedChunk``. So an unverified row cannot
reach the LLM, even through a refactor that forgets to call this function.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.policy import AuthUser, DocACL, can_access
from app.metrics import ACL_STALE, ACL_VIOLATIONS
from app.retrieval.retriever import RetrievedChunk

security_log = logging.getLogger("app.security")

_CONSTRUCTION_TOKEN = object()


class AuthorizedChunk:
    """A retrieved chunk that passed every post-retrieval check for one specific user.

    Immutable. Raises TypeError if constructed anywhere but ``authorize_chunks``.
    """

    __slots__ = ("_chunk", "user_id")

    def __init__(self, chunk: RetrievedChunk, user_id: uuid.UUID, *, _token: object) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise TypeError("AuthorizedChunk can only be created by authorize_chunks()")
        object.__setattr__(self, "_chunk", chunk)
        object.__setattr__(self, "user_id", user_id)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("AuthorizedChunk is immutable")

    def __repr__(self) -> str:  # no chunk text in reprs / logs
        return f"AuthorizedChunk(chunk_id={self.chunk_id}, doc_id={self.doc_id!r})"

    chunk_id = property(lambda self: self._chunk.chunk_id)
    doc_id = property(lambda self: self._chunk.doc_id)
    title = property(lambda self: self._chunk.title)
    chunk_index = property(lambda self: self._chunk.chunk_index)
    text = property(lambda self: self._chunk.text)
    distance = property(lambda self: self._chunk.distance)
    score = property(lambda self: self._chunk.score)


@dataclass(frozen=True)
class DroppedChunk:
    """A chunk removed by the post-retrieval check (ids only, never text)."""

    chunk_id: uuid.UUID
    doc_id: str
    reason: str


@dataclass(frozen=True)
class DocumentRecord:
    """The authoritative ACL of a document, as visible to the caller."""

    doc_id: str
    tenant_id: uuid.UUID
    classification: str
    allowed_roles: tuple[str, ...]
    allowed_users: tuple[uuid.UUID, ...]
    acl_version: int
    status: str = "ready"


DocLoader = Callable[[Session, Sequence[str]], dict[str, DocumentRecord]]


def load_document_acls(session: Session, doc_ids: Sequence[str]) -> dict[str, DocumentRecord]:
    """``documents`` rows for ``doc_ids`` in the caller's transaction (RLS applies)."""
    if not doc_ids:
        return {}
    rows = session.execute(
        text(
            "SELECT id, tenant_id, classification::text AS classification, allowed_roles, "
            "allowed_users, acl_version, status FROM documents WHERE id = ANY(:ids)"
        ),
        {"ids": list(doc_ids)},
    )
    return {
        r.id: DocumentRecord(
            r.id,
            r.tenant_id,
            r.classification,
            tuple(r.allowed_roles),
            tuple(r.allowed_users),
            r.acl_version,
            r.status,
        )
        for r in rows
    }


def _chunk_acl(c: RetrievedChunk) -> DocACL | None:
    if c.tenant_id is None:
        return None
    return DocACL(c.doc_id, c.tenant_id, c.classification, frozenset(c.allowed_roles),
                  frozenset(c.allowed_users))  # fmt: skip


def _doc_acl(d: DocumentRecord) -> DocACL:
    return DocACL(d.doc_id, d.tenant_id, d.classification, frozenset(d.allowed_roles),
                  frozenset(d.allowed_users))  # fmt: skip


def _reason(user: AuthUser, c: RetrievedChunk, doc: DocumentRecord | None) -> str | None:
    if not can_access(user, _chunk_acl(c)):
        return "policy_denied"
    if doc is None:
        return "document_missing"
    if doc.tenant_id != c.tenant_id:
        return "tenant_mismatch"
    if doc.status != "ready":
        return "document_not_ready"
    if doc.acl_version != c.acl_version:
        return "stale_acl"
    if not can_access(user, _doc_acl(doc)):
        return "document_policy_denied"
    return None


def authorize_chunks(
    session: Session,
    user: AuthUser,
    chunks: Sequence[RetrievedChunk],
    *,
    request_id: str = "-",
    doc_loader: DocLoader = load_document_acls,
) -> tuple[list[AuthorizedChunk], list[DroppedChunk]]:
    """Split ``chunks`` into authorized and dropped (see module docstring)."""
    docs = doc_loader(session, sorted({c.doc_id for c in chunks}))
    ok: list[AuthorizedChunk] = []
    dropped: list[DroppedChunk] = []
    for c in chunks:
        reason = _reason(user, c, docs.get(c.doc_id))
        if reason is None:
            ok.append(AuthorizedChunk(c, user.id, _token=_CONSTRUCTION_TOKEN))
            continue
        dropped.append(DroppedChunk(c.chunk_id, c.doc_id, reason))
        event = {
            "event_type": "stale_acl" if reason == "stale_acl" else "acl_violation",
            "reason": reason,
            "request_id": request_id,
            "user_id": str(user.id),
            "tenant_id": str(user.tenant_id),
            "chunk_id": str(c.chunk_id),
            "doc_id": c.doc_id,
            "chunk_tenant_id": str(c.tenant_id),
        }
        if reason == "stale_acl":
            ACL_STALE.inc()
            security_log.warning(
                "ACL_RESYNC_NEEDED %s (re-run scripts.ingest for this document)",
                json.dumps(event, sort_keys=True),
            )
        else:
            ACL_VIOLATIONS.inc(reason)
            security_log.error("SECURITY_ALERT %s", json.dumps(event, sort_keys=True))
    return ok, dropped
