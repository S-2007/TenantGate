"""GET /admin/audit/documents/{doc_id}: who retrieved this document, and when.

Authorization:
- the caller must be authenticated AND hold the ``admin`` role, otherwise 403;
- the document must belong to the caller's tenant, otherwise **404** (not 403), so the response
  never says whether another tenant has a document with that id;
- the audit rows themselves are tenant-filtered by RLS and by an explicit WHERE.
An admin may audit a confidential document it cannot read: the response lists who retrieved it
(user id, time, query hash, request id), never its content.

Pagination is keyset on (ts DESC, id DESC), with an opaque cursor.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text

from app.auth.dependencies import get_current_user
from app.auth.policy import AuthUser
from app.db.rls import apply_rls_context
from app.db.session import transaction

router = APIRouter(prefix="/admin/audit", tags=["admin"])


class AccessEntry(BaseModel):
    user_id: uuid.UUID
    ts: dt.datetime
    query_hash: str | None
    request_id: uuid.UUID


class DocumentAccessPage(BaseModel):
    doc_id: str
    days: int
    items: list[AccessEntry]
    next_cursor: str | None


def _encode_cursor(ts: dt.datetime, row_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{row_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[dt.datetime, uuid.UUID]:
    try:
        ts_s, id_s = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
        return dt.datetime.fromisoformat(ts_s), uuid.UUID(id_s)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid cursor") from exc


def require_admin(user: AuthUser = Depends(get_current_user)) -> AuthUser:  # noqa: B008
    """403 unless the (DB-confirmed) caller holds the admin role."""
    if "admin" not in user.roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return user


@router.get("/documents/{doc_id}", response_model=DocumentAccessPage)
def document_access(
    doc_id: str,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, max_length=200),
    admin: AuthUser = Depends(require_admin),  # noqa: B008
) -> DocumentAccessPage:
    """Retrievals of ``doc_id`` by anyone in the admin's tenant over the last ``days`` days."""
    if len(doc_id) > 200:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    after = _decode_cursor(cursor) if cursor else None
    with transaction() as s:
        if not apply_rls_context(s, admin):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
        exists = s.execute(text("SELECT document_in_current_tenant(:d)"), {"d": doc_id}).scalar()
        if not exists:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
        rows = s.execute(
            text(
                """
                SELECT id, user_id, ts, query_hash, request_id
                FROM retrieval_audit
                WHERE tenant_id = :tenant
                  AND event_type = 'retrieval'
                  AND returned_doc_ids @> ARRAY[CAST(:doc AS text)]
                  AND ts >= now() - make_interval(days => :days)
                  AND (CAST(:after_ts AS timestamptz) IS NULL
                       OR (ts, id) < (CAST(:after_ts AS timestamptz), CAST(:after_id AS uuid)))
                ORDER BY ts DESC, id DESC
                LIMIT :lim
                """
            ),
            {
                "tenant": admin.tenant_id,
                "doc": doc_id,
                "days": days,
                "after_ts": after[0] if after else None,
                "after_id": after[1] if after else None,
                "lim": limit + 1,
            },
        ).all()
    page, more = rows[:limit], len(rows) > limit
    return DocumentAccessPage(
        doc_id=doc_id,
        days=days,
        items=[
            AccessEntry(
                user_id=r.user_id, ts=r.ts, query_hash=r.query_hash, request_id=r.request_id
            )
            for r in page
        ],
        next_cursor=_encode_cursor(page[-1].ts, page[-1].id) if more and page else None,
    )
