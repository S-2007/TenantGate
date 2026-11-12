"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.audit.log import AuditRecord, status_for, write_audit
from app.config import get_settings
from app.db.health import probe_database
from app.db.session import reset_engine
from app.metrics import ASK_REQUESTS, AUDIT_WRITE_FAILURES, render_all
from app.request_context import REQUEST_ID, install_log_filter, new_request_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s [req=%(request_id)s]: %(message)s",
)
install_log_filter()
logger = logging.getLogger("app")
security_log = logging.getLogger("app.security")

# Identity must come from the verified token only. These are ignored, but a client sending them
# is trying (or misconfigured) to assert identity, so it is logged.
IDENTITY_HINT_HEADERS = ("x-tenant-id", "x-tenant", "x-user-id", "x-user", "x-roles", "x-role")
IDENTITY_HINT_PARAMS = ("tenant_id", "tenant", "user_id", "roles", "role")

WEB_DIR = Path(__file__).parent / "web"
# The UI loads only its own files: no inline script, no third-party code, no framing.
UI_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Fail fast on bad config at startup; dispose the pool on shutdown."""
    get_settings().validate_for_app()
    from app.auth.passwords import dummy_hash

    dummy_hash()  # pay the one-off hashing cost now, not on the first unknown-email login
    s = get_settings()
    if s.embedding_model != "hash":
        from app.ingestion.embedder import get_embedder

        # Load the model at startup so the first user's question isn't the one that waits.
        await run_in_threadpool(get_embedder, s.embedding_model, s.embedding_dim)
    yield
    reset_engine()


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(title="Permission-aware multi-tenant RAG", version="0.1.0", lifespan=lifespan)
    app.state.audit_writer = write_audit  # swapped for an in-memory list in unit tests
    ui_origin = get_settings().ui_origin.strip()
    if ui_origin:  # Phase 6: a UI served from another origin. Bearer tokens only, no cookies.
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=[ui_origin],
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Retry-After"],
            max_age=600,
        )

    @app.get("/health")
    def health() -> JSONResponse:
        """DB reachability + latency, pgvector presence, and embedding-dimension consistency."""
        settings = get_settings()
        db = probe_database(settings.embedding_dim)
        vector_ok = bool(db.get("vector_extension", {}).get("installed"))
        table = db.get("chunks_table", {})
        if not db["ok"]:
            status, code = "unhealthy", 503
        elif not vector_ok or not table.get("exists") or not table.get("dim_matches_config"):
            status, code = "degraded", 503
        else:
            status, code = "healthy", 200
        return JSONResponse(
            status_code=code,
            content={
                "status": status,
                "env": settings.env,
                "database": db,
                "embedding": {"model": settings.embedding_model, "dim": settings.embedding_dim},
                "llm": {
                    "provider": settings.llm_provider,
                    "model": settings.llm_model,
                    "fallbacks": [f"{p}:{m}" for p, m in settings.answer_specs()[1:]],
                },
            },
        )

    @app.middleware("http")
    async def flag_identity_hints(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Log (never honour) client-supplied identity in headers or query string."""
        hdrs = [h for h in IDENTITY_HINT_HEADERS if h in request.headers]
        params = [p for p in IDENTITY_HINT_PARAMS if p in request.query_params]
        if hdrs or params:
            security_log.warning(
                "suspicious_request reason=client_supplied_identity path=%s headers=%s params=%s "
                "ip=%s (ignored)",
                request.url.path,
                hdrs,
                params,
                request.client.host if request.client else "?",
            )
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):  # type: ignore[no-untyped-def]
        """Standard 422, plus a security log line when the body carried forbidden fields."""
        extra = sorted(
            str(e["loc"][-1]) for e in exc.errors() if e.get("type") == "extra_forbidden"
        )
        if extra:
            security_log.warning(
                "suspicious_request reason=forbidden_body_fields path=%s fields=%s ip=%s",
                request.url.path,
                extra,
                request.client.host if request.client else "?",
            )
        return await request_validation_exception_handler(request, exc)

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> PlainTextResponse:
        """Prometheus text format. Counters carry no user, tenant or document identifiers."""
        return PlainTextResponse(render_all(), media_type="text/plain; version=0.0.4")

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Outermost middleware: request id, generic 500s, and the /ask audit row (fail closed).

        Exactly one audit row per POST /ask, whatever happened: the endpoint fills
        ``request.state.audit`` as it goes. A request rejected before reaching the endpoint (401,
        422) gets a row built from the status code alone. If the row can't be written, the
        response is replaced by a 503, so an unaudited answer never leaves the server.
        """
        rid = new_request_id(request.headers.get("x-request-id"))
        token = REQUEST_ID.set(rid)
        request.state.request_id = rid
        is_ask = request.method == "POST" and request.url.path == "/ask"
        try:
            try:
                response = await call_next(request)
            except Exception as exc:  # noqa: BLE001 - never echo internals to the client
                logger.error("unhandled %s on %s", type(exc).__name__, request.url.path)
                response = JSONResponse(
                    status_code=500, content={"detail": "internal error", "request_id": rid}
                )
            if is_ask:
                rec = getattr(request.state, "audit", None) or AuditRecord(request_id=rid)
                rec.http_status = response.status_code
                if rec.status is None or (rec.status == "ok" and response.status_code >= 400):
                    rec.status = status_for(response.status_code)
                try:
                    await run_in_threadpool(request.app.state.audit_writer, rec)
                    ASK_REQUESTS.inc(rec.status)
                except Exception as exc:  # noqa: BLE001 - any audit failure fails the request
                    AUDIT_WRITE_FAILURES.inc()
                    logger.error("audit write failed (%s); answer withheld", type(exc).__name__)
                    response = JSONResponse(
                        status_code=503,
                        content={
                            "detail": "audit unavailable; request not served",
                            "request_id": rid,
                        },
                    )
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            REQUEST_ID.reset(token)

    from app.admin.api import router as admin_router  # noqa: E402
    from app.api import router  # noqa: E402
    from app.audit.api import router as audit_router  # noqa: E402
    from app.auth.router import router as auth_router  # noqa: E402
    from app.tenants.api import router as tenants_router  # noqa: E402
    from app.uploads.api import router as uploads_router  # noqa: E402

    app.include_router(auth_router)
    app.include_router(router)
    app.include_router(audit_router)
    app.include_router(admin_router)
    app.include_router(tenants_router)
    app.include_router(uploads_router)

    # Minimal web UI: static files only, no external scripts, strict CSP.
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def web_ui() -> FileResponse:
        """The single-page UI (talks to this same API with a bearer token kept in memory)."""
        return FileResponse(WEB_DIR / "index.html", headers=UI_SECURITY_HEADERS)

    return app


app = create_app()
