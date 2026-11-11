"""SQLAlchemy engine/session factory for Neon serverless Postgres.

Neon specifics handled here:
- Scale-to-zero: the first connection after idle can take seconds or fail once while the compute
  wakes. Connections are opened through ``_connect_with_retry`` (exponential backoff) and the
  latency of every physical connect is recorded in ``CONNECT_STATS`` and logged (host only).
- Pooled endpoint = PgBouncer in *transaction* mode: no session state survives between
  transactions, so psycopg auto-prepared statements are disabled (``prepare_threshold=None``) and
  any per-request setting must use ``SET LOCAL`` / ``set_config(..., true)`` inside ``transaction()``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import psycopg
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings, url_host

logger = logging.getLogger(__name__)

CONNECT_ATTEMPTS = 4
CONNECT_BACKOFF_S = 0.5  # 0.5, 1, 2 seconds between attempts
CONNECT_TIMEOUT_S = 10


@dataclass
class ConnectStats:
    """Latency of physical connections opened by this process (milliseconds)."""

    first_connect_ms: float | None = None
    last_connect_ms: float | None = None
    total_connects: int = 0
    retries: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, ms: float, retries: int) -> None:
        with self._lock:
            if self.first_connect_ms is None:
                self.first_connect_ms = ms
            self.last_connect_ms = ms
            self.total_connects += 1
            self.retries += retries

    def snapshot(self) -> dict[str, float | int | None]:
        with self._lock:
            return {
                "first_connect_ms": self.first_connect_ms,
                "last_connect_ms": self.last_connect_ms,
                "total_connects": self.total_connects,
                "retries": self.retries,
            }


CONNECT_STATS = ConnectStats()


def _libpq_dsn(sqlalchemy_url: str) -> str:
    """Convert ``postgresql+psycopg://`` back to a libpq URI that psycopg.connect accepts."""
    p = urlsplit(sqlalchemy_url)
    return urlunsplit(("postgresql", p.netloc, p.path, p.query, p.fragment))


def connect_with_retry(
    dsn: str,
    *,
    attempts: int = CONNECT_ATTEMPTS,
    backoff_s: float = CONNECT_BACKOFF_S,
    stats: ConnectStats | None = CONNECT_STATS,
) -> psycopg.Connection:
    """Open a psycopg connection, retrying ``OperationalError`` with exponential backoff.

    Only the host is ever logged; the DSN (which contains the password) never is.
    """
    host = url_host(dsn)
    last_exc: Exception | None = None
    for attempt in range(attempts):
        t0 = time.perf_counter()
        try:
            conn = psycopg.connect(
                dsn, connect_timeout=CONNECT_TIMEOUT_S, prepare_threshold=None, autocommit=False
            )
        except psycopg.OperationalError as exc:
            last_exc = exc
            wait = backoff_s * (2**attempt)
            logger.warning(
                "db connect to %s failed (attempt %d/%d, %s); retrying in %.1fs",
                host,
                attempt + 1,
                attempts,
                type(exc).__name__,
                wait,
            )
            if attempt + 1 < attempts:
                time.sleep(wait)
            continue
        ms = (time.perf_counter() - t0) * 1000
        if stats is not None:
            stats.record(ms, retries=attempt)
        logger.info("db connected to %s in %.0f ms (retries=%d)", host, ms, attempt)
        return conn
    raise ConnectionError(
        f"could not connect to database host {host} after {attempts} attempts"
    ) from last_exc


def build_engine(sqlalchemy_url: str, *, pool_size: int = 3, max_overflow: int = 2) -> Engine:
    """Create an engine with a small pool, pre-ping and the retrying connect function."""
    dsn = _libpq_dsn(sqlalchemy_url)
    return create_engine(
        "postgresql+psycopg://",
        creator=lambda: connect_with_retry(dsn),
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=300,  # Neon/PgBouncer may drop idle server connections
        pool_timeout=15,
    )


_engine: Engine | None = None
_engine_lock = threading.Lock()
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Lazily-built process-wide engine for the *pooled* app URL."""
    global _engine, _SessionLocal
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                s = get_settings()
                _engine = build_engine(
                    s.sqlalchemy_url, pool_size=s.db_pool_size, max_overflow=s.db_max_overflow
                )
                _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    """Dispose the engine (used by tests and on shutdown)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


_admin_engine: Engine | None = None
_AdminSession: sessionmaker[Session] | None = None


def get_admin_engine() -> Engine:
    """Engine for the OWNER role over DATABASE_URL_DIRECT. Admin scripts and tests only.

    Seed, ingest and verify_index write or audit the whole index, so they run as the table owner
    on the direct (non-pooled) endpoint. The running API never calls this.
    """
    global _admin_engine, _AdminSession
    if _admin_engine is None:
        with _engine_lock:
            if _admin_engine is None:
                s = get_settings()
                s.validate_for_migrations()
                _admin_engine = build_engine(s.sqlalchemy_url_direct, pool_size=1, max_overflow=1)
                _AdminSession = sessionmaker(bind=_admin_engine, expire_on_commit=False)
    return _admin_engine


def reset_admin_engine() -> None:
    """Dispose the admin engine (tests / end of a script)."""
    global _admin_engine, _AdminSession
    if _admin_engine is not None:
        _admin_engine.dispose()
    _admin_engine = None
    _AdminSession = None


@contextmanager
def admin_transaction() -> Iterator[Session]:
    """Like ``transaction()`` but as the owner role on the direct URL (admin work only)."""
    get_admin_engine()
    assert _AdminSession is not None
    session = _AdminSession()
    try:
        with session.begin():
            yield session
    finally:
        session.close()


@contextmanager
def transaction() -> Iterator[Session]:
    """One unit of work = one DB transaction. Commits on success, rolls back on any error.

    Anything request-scoped (later: RLS tenant/user settings via ``set_config(..., true)``) must be
    issued inside this block so it is bound to the same transaction as the query.
    """
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        with session.begin():
            yield session
    finally:
        session.close()
