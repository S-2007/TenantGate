"""Document upload endpoints (Phase 6).

POST   /documents/upload             multipart: file + title + classification + ACL -> 202
GET    /documents/upload-info        may I upload, which types, size limit
GET    /documents/mine               my uploads (any status)
GET    /documents/{doc_id}           metadata (uploader or a reader), else 404
GET    /documents/{doc_id}/status    processing | ready | failed (+ last_error), else 404
PUT    /documents/{doc_id}/content   replace the file (uploader or admin) -> 202
DELETE /documents/{doc_id}           document + all chunks (uploader or admin)

Order of checks on upload, cheapest first, nothing trusted from the client:
 1. authenticated (401), rate limit (429), role may upload (403, re-checked by the database)
 2. Content-Length over the limit -> 413 before reading the body
 3. the body is streamed through a byte counter while being parsed; past the limit -> 413
    (a missing or lying Content-Length doesn't help: the counter is what stops it)
 4. one file part only; metadata fields validated by Pydantic; type sniffed by magic bytes
 5. documents row created by the database (tenant from the RLS identity, quota, ACL shape,
    allowed_users must be active users of the same tenant), then extraction runs in the
    background and the chunks appear atomically with status 'ready'.
Unknown, foreign-tenant and not-yours documents all get the same 404.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.admin.acl import AclChangeError, call_definer
from app.auth.dependencies import get_current_user
from app.auth.policy import KNOWN_ROLES, AuthUser
from app.config import get_settings
from app.db.rls import apply_rls_context
from app.db.session import transaction
from app.ratelimit import enforce
from app.uploads import service
from app.uploads.extract import CONTENT_TYPES, UploadRejected, extension, safe_filename, sniff

security_log = logging.getLogger("app.security")
router = APIRouter(prefix="/documents", tags=["documents"])

RoleName = Literal[tuple(sorted(KNOWN_ROLES))]  # type: ignore[valid-type]
MULTIPART_OVERHEAD = 64 * 1024  # boundaries + small text fields
NOT_FOUND = HTTPException(status.HTTP_404_NOT_FOUND, "not found")


class UploadForm(BaseModel):
    """Metadata fields of the multipart form. Unknown fields (tenant_id, ...) -> 422."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    classification: Literal["public", "internal", "confidential"]
    allowed_roles: list[RoleName] = Field(default_factory=list, max_length=len(KNOWN_ROLES))
    allowed_users: list[uuid.UUID] = Field(default_factory=list, max_length=200)

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v or any(ord(c) < 32 for c in v):
            raise ValueError("title contains characters that are not allowed")
        return v


class UploadAccepted(BaseModel):
    doc_id: str
    status: str
    classification: str
    acl_version: int


class DocumentMeta(BaseModel):
    doc_id: str
    title: str
    classification: str
    status: str
    created_at: dt.datetime
    updated_at: dt.datetime
    acl_version: int
    chunk_count: int | None
    size_bytes: int | None
    original_filename: str | None
    last_error: str | None
    uploaded_by_me: bool


class DocumentStatus(BaseModel):
    doc_id: str
    status: str
    chunk_count: int | None
    last_error: str | None
    updated_at: dt.datetime


def _split(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        out.extend(p.strip() for p in v.split(",") if p.strip())
    return out


def _require_upload_role(user: AuthUser) -> None:
    if not (user.roles & get_settings().upload_role_set):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role may not upload documents")


async def _read_form(request: Request, *, need_meta: bool) -> tuple[UploadFile, UploadForm | None]:
    """Stream + parse the multipart body under a hard byte limit."""
    s = get_settings()
    limit = s.max_upload_bytes + MULTIPART_OVERHEAD
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"file is larger than {s.max_upload_mb:g} MB")  # fmt: skip
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "send multipart/form-data")

    async def limited() -> AsyncGenerator[bytes, None]:
        seen = 0
        async for chunk in request.stream():
            seen += len(chunk)
            if seen > limit:
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                    f"file is larger than {s.max_upload_mb:g} MB")  # fmt: skip
            yield chunk

    parser = MultiPartParser(request.headers, limited(), max_files=1, max_fields=20,
                             max_part_size=16 * 1024)  # fmt: skip
    try:
        form = await parser.parse()
    except MultiPartException as exc:
        raise HTTPException(422, "malformed upload") from exc
    files = [v for _, v in form.multi_items() if isinstance(v, UploadFile)]
    if len(files) != 1 or [k for k, v in form.multi_items() if isinstance(v, UploadFile)] != [
        "file"
    ]:
        for f in files:
            await f.close()
        raise HTTPException(422, "send exactly one 'file' part")
    up = files[0]
    if (up.size or 0) > s.max_upload_bytes:
        await up.close()
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"file is larger than {s.max_upload_mb:g} MB")  # fmt: skip
    if not up.size:
        await up.close()
        raise HTTPException(422, "the file is empty")
    fields: dict[str, list[str]] = {}
    for k, v in form.multi_items():
        if isinstance(v, str):
            fields.setdefault(k, []).append(v)
    meta = None
    if need_meta:
        raw: dict[str, object] = {k: v[-1] for k, v in fields.items()}
        for k in ("allowed_roles", "allowed_users"):
            if k in fields:
                raw[k] = _split(fields[k])
        raw.setdefault("title", (up.filename or "").rsplit(".", 1)[0][:200] or "Untitled")
        try:
            meta = UploadForm.model_validate(raw)
        except ValidationError as exc:
            await up.close()
            extra = [str(e["loc"][-1]) for e in exc.errors() if e["type"] == "extra_forbidden"]
            if extra:
                security_log.warning("suspicious_request reason=forbidden_upload_fields "
                                     "fields=%s", sorted(extra))  # fmt: skip
            detail = [{"loc": ["body", *e["loc"]], "msg": e["msg"], "type": e["type"]}
                      for e in exc.errors()]  # fmt: skip
            raise HTTPException(422, detail) from exc
    elif fields:
        await up.close()
        raise HTTPException(422,
                            "only the file can be replaced (change the ACL with PATCH .../acl)")  # fmt: skip
    return up, meta


async def _sniff(up: UploadFile) -> str:
    s = get_settings()
    head = await up.read(8)
    await up.seek(0)
    try:
        return sniff(head, extension(up.filename or ""), s.upload_types)
    except UploadRejected as exc:
        await up.close()
        raise HTTPException(exc.status, exc.message) from exc


def _db_error(exc: AclChangeError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


@router.post("/upload", response_model=UploadAccepted, status_code=202)
async def upload(
    request: Request,
    background: BackgroundTasks,
    user: AuthUser = Depends(get_current_user),  # noqa: B008
) -> UploadAccepted:
    """Accept a file; it becomes searchable when its status is 'ready'."""
    enforce(request, "upload", get_settings().upload_rate_per_minute, 60)
    _require_upload_role(user)
    up, meta = await _read_form(request, need_meta=True)
    assert meta is not None
    kind = await _sniff(up)
    filename = safe_filename(up.filename or f"upload.{kind}")
    size = int(up.size or 0)
    try:
        out = await run_in_threadpool(
            service.begin, user, service.UploadMeta(meta.title, meta.classification,
                                                    meta.allowed_roles, meta.allowed_users),
            size=size, content_type=CONTENT_TYPES[kind], filename=filename,
            request_id=request.state.request_id,
        )  # fmt: skip
    except AclChangeError as exc:
        await up.close()
        raise _db_error(exc) from exc
    background.add_task(service.process, user, out["doc_id"], meta.title, up.file,
                        filename=filename, size=size, replace=False,
                        request_id=request.state.request_id)  # fmt: skip
    return UploadAccepted(doc_id=out["doc_id"], status="processing",
                          classification=out["classification"], acl_version=out["acl_version"])  # fmt: skip


def _meta_rows(sql: str, user: AuthUser, params: dict) -> list:
    with transaction() as s:
        if not apply_rls_context(s, user):
            return []
        return s.execute(text(sql), params).mappings().all()


class UploadInfo(BaseModel):
    can_upload: bool
    allowed_types: list[str]
    max_upload_mb: float


@router.get("/upload-info", response_model=UploadInfo)
def upload_info(user: AuthUser = Depends(get_current_user)) -> UploadInfo:  # noqa: B008
    """What the UI needs to show the upload form (the server enforces all of it anyway)."""
    s = get_settings()
    return UploadInfo(can_upload=bool(user.roles & s.upload_role_set),
                      allowed_types=sorted(s.upload_types), max_upload_mb=s.max_upload_mb)  # fmt: skip


@router.get("/mine", response_model=list[DocumentMeta])
def mine(user: AuthUser = Depends(get_current_user)) -> list[DocumentMeta]:  # noqa: B008
    """Documents I uploaded, in any status (newest first)."""
    return [DocumentMeta(**r) for r in _meta_rows("SELECT * FROM my_uploads()", user, {})]


def _one(doc_id: str, user: AuthUser) -> DocumentMeta:
    if len(doc_id) > 200:
        raise NOT_FOUND
    rows = _meta_rows("SELECT * FROM document_meta(:d)", user, {"d": doc_id})
    if not rows:
        raise NOT_FOUND
    return DocumentMeta(**rows[0])


@router.get("/{doc_id}", response_model=DocumentMeta)
def get_document(doc_id: str, user: AuthUser = Depends(get_current_user)) -> DocumentMeta:  # noqa: B008
    """Metadata of a document I uploaded or may read. Never its text or ACL lists."""
    return _one(doc_id, user)


@router.get("/{doc_id}/status", response_model=DocumentStatus)
def get_status(doc_id: str, user: AuthUser = Depends(get_current_user)) -> DocumentStatus:  # noqa: B008
    m = _one(doc_id, user)
    return DocumentStatus(doc_id=m.doc_id, status=m.status, chunk_count=m.chunk_count,
                          last_error=m.last_error, updated_at=m.updated_at)  # fmt: skip


@router.put("/{doc_id}/content", response_model=UploadAccepted, status_code=202)
async def replace(
    doc_id: str,
    request: Request,
    background: BackgroundTasks,
    user: AuthUser = Depends(get_current_user),  # noqa: B008
) -> UploadAccepted:
    """Replace a ready document's file. Old chunks stay searchable until the new ones are in."""
    enforce(request, "upload", get_settings().upload_rate_per_minute, 60)
    _require_upload_role(user)
    meta = await run_in_threadpool(_manageable, doc_id, user)
    up, _ = await _read_form(request, need_meta=False)
    kind = await _sniff(up)
    filename = safe_filename(up.filename or f"upload.{kind}")
    background.add_task(service.process, user, doc_id, meta.title, up.file, filename=filename,
                        size=int(up.size or 0), replace=True,
                        request_id=request.state.request_id)  # fmt: skip
    return UploadAccepted(doc_id=doc_id, status="processing", classification=meta.classification,
                          acl_version=meta.acl_version)  # fmt: skip


def _manageable(doc_id: str, user: AuthUser) -> DocumentMeta:
    """404 unless the caller uploaded it or is an admin of its tenant (same as the DB rule)."""
    m = _one(doc_id, user)
    if not (m.uploaded_by_me or "admin" in user.roles):
        raise NOT_FOUND
    if m.status != "ready":
        raise HTTPException(status.HTTP_409_CONFLICT, f"document is {m.status}; wait or delete it")
    return m


@router.delete("/{doc_id}")
def delete(doc_id: str, request: Request, user: AuthUser = Depends(get_current_user)) -> dict:  # noqa: B008
    """Delete a document and all its chunks (uploader or admin). Audited."""
    if len(doc_id) > 200:
        raise NOT_FOUND
    try:
        return call_definer(user, "SELECT delete_document(:d, CAST(:rid AS uuid))",
                            {"d": doc_id, "rid": request.state.request_id})  # fmt: skip
    except AclChangeError as exc:
        raise _db_error(exc) from exc
