"""Upload lifecycle on top of the SECURITY DEFINER functions of migration 0007 (Phase 6).

    begin   (request)     documents row, status 'processing', ACL validated by the database
    process (background)  extract -> sanitise -> chunk -> embed -> complete (chunks + 'ready')
                          or fail (status 'failed' + last_error), always as the uploader

The ACL of every stored chunk is copied by the database from the documents row inside
``upload_document_complete``; this module never sends an ACL with the chunks, so a bug here
can't give a chunk a wider ACL than its document. Chunks only become visible when the status
flips to 'ready', in the same transaction that inserts them.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import BinaryIO

from app.admin.acl import AclChangeError, call_definer
from app.auth.policy import AuthUser
from app.config import Settings, get_settings
from app.ingestion.chunker import chunk_text, embedding_input
from app.ingestion.embedder import Embedder, get_embedder
from app.uploads.extract import Limits, UploadRejected, extract

logger = logging.getLogger(__name__)
security_log = logging.getLogger("app.security")

# Last ingestion timings (ms), for the benchmark / logs. Process-local, ids only.
LAST_TIMINGS: dict[str, dict[str, float]] = {}


@dataclass(frozen=True)
class UploadMeta:
    title: str
    classification: str
    allowed_roles: Sequence[str]
    allowed_users: Sequence[uuid.UUID]


def new_doc_id() -> str:
    """Server-generated, globally unique document id (never chosen by the client)."""
    return f"up-{uuid.uuid4().hex}"


def limits(s: Settings) -> Limits:
    return Limits(
        max_bytes=s.max_upload_bytes,
        max_pdf_pages=s.max_pdf_pages,
        max_chars=s.max_extracted_chars,
        max_docx_uncompressed=int(s.max_docx_uncompressed_mb * 1024 * 1024),
    )


def begin(
    user: AuthUser, meta: UploadMeta, *, size: int, content_type: str, filename: str,
    request_id: str, doc_id: str | None = None,
) -> dict:  # fmt: skip
    """Create the documents row (status 'processing'). Raises AclChangeError (403/404/409/422)."""
    s = get_settings()
    return call_definer(
        user,
        "SELECT upload_document_begin(:doc, :title, CAST(:cls AS doc_classification), "
        "CAST(:roles AS text[]), CAST(:users AS uuid[]), CAST(:uroles AS text[]), :maxdocs, "
        ":size, :ctype, :fname, CAST(:rid AS uuid))",
        {"doc": doc_id or new_doc_id(), "title": meta.title, "cls": meta.classification,
         "roles": list(meta.allowed_roles), "users": [str(u) for u in meta.allowed_users],
         "uroles": sorted(s.upload_role_set), "maxdocs": s.max_documents_per_tenant,
         "size": size, "ctype": content_type, "fname": filename, "rid": request_id},
    )  # fmt: skip


def _fail(user: AuthUser, doc_id: str, message: str, *, replace: bool, request_id: str) -> None:
    try:
        call_definer(
            user,
            "SELECT upload_document_fail(:doc, :err, :rep, CAST(:rid AS uuid))",
            {"doc": doc_id, "err": message[:300], "rep": replace, "rid": request_id},
        )
    except Exception as exc:  # noqa: BLE001 - e.g. uploader deactivated meanwhile
        # verify_index reports documents stuck in 'processing'; nothing is retrievable meanwhile
        logger.error("could not record upload failure for %s (%s)", doc_id, type(exc).__name__)


def process(
    user: AuthUser, doc_id: str, title: str, f: BinaryIO, *, filename: str, size: int,
    replace: bool, request_id: str, embedder: Embedder | None = None,
) -> str:  # fmt: skip
    """Background step. Returns the final status ('ready' / 'failed' / kept status on replace)."""
    s = get_settings()
    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    try:
        try:
            ex = extract(f, filename, s.upload_types, limits(s))
        finally:
            f.close()
        timings["extract_ms"] = (time.perf_counter() - t0) * 1000
        emb = embedder or get_embedder(s.embedding_model, s.embedding_dim)
        chunks = chunk_text(ex.text, doc_id=doc_id, title=title, tokenizer=emb.tokenizer,
                            size=s.chunk_size, overlap=s.chunk_overlap)  # fmt: skip
        if not chunks:
            raise UploadRejected("no readable text found in the document")
        if len(chunks) > s.max_chunks_per_tenant:
            raise UploadRejected("document is too large for this company's chunk quota", 409)
        t1 = time.perf_counter()
        vecs = emb.embed_documents([embedding_input(c) for c in chunks])
        timings["embed_ms"] = (time.perf_counter() - t1) * 1000
        payload = [
            {"chunk_id": str(c.chunk_id), "chunk_index": c.chunk_index, "text": c.text,
             "embedding": "[" + ",".join(f"{x:.7g}" for x in v) + "]"}
            for c, v in zip(chunks, vecs, strict=True)
        ]  # fmt: skip
        t2 = time.perf_counter()
        call_definer(
            user,
            "SELECT upload_document_complete(:doc, CAST(:chunks AS jsonb), :maxc, :rep, :size, "
            ":ctype, :fname, CAST(:rid AS uuid))",
            {"doc": doc_id, "chunks": json.dumps(payload), "maxc": s.max_chunks_per_tenant,
             "rep": replace, "size": size, "ctype": ex.content_type, "fname": filename,
             "rid": request_id},
        )  # fmt: skip
        timings["store_ms"] = (time.perf_counter() - t2) * 1000
        timings["total_ms"] = (time.perf_counter() - t0) * 1000
        timings["chunks"] = float(len(chunks))
        LAST_TIMINGS[doc_id] = timings
        logger.info("upload %s ready: %d chunks in %.0f ms", doc_id, len(chunks),
                    timings["total_ms"])  # fmt: skip
        return "ready"
    except UploadRejected as exc:
        security_log.info("upload_rejected doc=%s reason=%s", doc_id, exc.message)
        _fail(user, doc_id, exc.message, replace=replace, request_id=request_id)
    except AclChangeError as exc:  # quota at completion time, uploader lost rights, ...
        _fail(user, doc_id, exc.message, replace=replace, request_id=request_id)
    except Exception as exc:  # noqa: BLE001 - never leave a document silently processing
        logger.error("upload %s failed: %s", doc_id, type(exc).__name__)
        _fail(user, doc_id, "processing failed", replace=replace, request_id=request_id)
    return "kept" if replace else "failed"
