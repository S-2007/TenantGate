"""Append-only retrieval audit: exactly one row per POST /ask (success, denial or error).

- Written through the app's low-privilege role inside a transaction bound to the caller's
  tenant (RLS ``WITH CHECK``). Unauthenticated denials are written with user and tenant NULL.
- FAIL CLOSED: if the row can't be written, the answer is not returned (503). An unaudited
  answer is the thing the audit log exists to prevent (D-031).
- ``query_hash`` is HMAC-SHA256 of the normalised question, keyed from JWT_SECRET (domain-
  separated). A plain SHA-256 of a short question could be reversed by guessing.
  ``STORE_QUERY_TEXT=false`` stores only the hash.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.auth.policy import AuthUser
from app.config import get_settings
from app.db.rls import apply_rls_context
from app.db.session import transaction

_HASH_DOMAIN = b"rag-platform/audit-query-hash/v1"


def query_hash(question: str, secret: str) -> str:
    """Keyed, normalised hash of a question (whitespace collapsed, case-folded)."""
    key = hmac.new(secret.encode(), _HASH_DOMAIN, hashlib.sha256).digest()
    norm = " ".join(question.split()).casefold()
    return hmac.new(key, norm.encode(), hashlib.sha256).hexdigest()


@dataclass
class AuditRecord:
    """Everything one audit row needs; filled progressively during the request."""

    request_id: str
    user: AuthUser | None = None
    question: str | None = None
    returned_chunk_ids: list[uuid.UUID] = field(default_factory=list)
    returned_doc_ids: list[str] = field(default_factory=list)
    dropped_chunk_ids: list[uuid.UUID] = field(default_factory=list)
    model: str | None = None
    latency_ms: float | None = None
    status: str | None = None  # ok | denied | error; derived from the HTTP status if unset
    http_status: int | None = None
    event_type: str = "retrieval"


def status_for(http_status: int) -> str:
    """Audit status for a response code."""
    if http_status < 400:
        return "ok"
    if http_status in (401, 403, 404, 422):
        return "denied"
    return "error"


def write_audit(rec: AuditRecord) -> None:
    """Insert one row (raises on any failure; the caller fails closed)."""
    s = get_settings()
    status = rec.status or status_for(rec.http_status or 500)
    q_text = rec.question if (rec.question is not None and s.store_query_text) else None
    q_hash = query_hash(rec.question, s.jwt_secret) if rec.question is not None else None
    user = rec.user if (rec.user is not None and rec.user.is_active) else None
    # Retried once if the connection drops. If the first commit succeeded but its ack was lost,
    # the retry writes a second row: over-auditing is the safe failure mode (D-035).
    for attempt in (1, 2):
        try:
            with transaction() as session:
                if user is not None:
                    apply_rls_context(session, user)
                session.execute(
                    text(
                        """
                        INSERT INTO retrieval_audit (
                            request_id, event_type, user_id, tenant_id, roles, query_text, query_hash,
                            returned_chunk_ids, returned_doc_ids, dropped_chunk_ids, model, latency_ms,
                            status, http_status)
                        VALUES (
                            :request_id, :event_type, :user_id, :tenant_id, CAST(:roles AS text[]),
                            :query_text, :query_hash, CAST(:chunks AS uuid[]), CAST(:docs AS text[]),
                            CAST(:dropped AS uuid[]), :model, :latency_ms, :status, :http_status)
                        """
                    ),
                    {
                        "request_id": uuid.UUID(rec.request_id),
                        "event_type": rec.event_type,
                        "user_id": user.id if user else None,
                        "tenant_id": user.tenant_id if user else None,
                        "roles": sorted(user.roles) if user else [],
                        "query_text": q_text,
                        "query_hash": q_hash,
                        "chunks": [str(c) for c in rec.returned_chunk_ids],
                        "docs": list(dict.fromkeys(rec.returned_doc_ids)),
                        "dropped": [str(c) for c in rec.dropped_chunk_ids],
                        "model": rec.model,
                        "latency_ms": rec.latency_ms,
                        "status": status,
                        "http_status": rec.http_status,
                    },
                )
            return
        except OperationalError:
            if attempt == 2:
                raise
