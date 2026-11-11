"""Database health probe used by GET /health."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import text

from app.db.session import CONNECT_STATS, transaction


def probe_database(expected_dim: int) -> dict[str, Any]:
    """Run SELECT 1, check the vector extension and the chunks.embedding dimension.

    Never raises: returns ``{"ok": False, "error": <exception class>}`` on failure so that no
    connection details or messages leak into the response.
    """
    result: dict[str, Any] = {"ok": False}
    t0 = time.perf_counter()
    try:
        with transaction() as s:
            s.execute(text("SELECT 1")).scalar_one()
            result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            ver = s.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar_one_or_none()
            result["vector_extension"] = {"installed": ver is not None, "version": ver}
            # For pgvector columns, atttypmod holds the declared dimension.
            dim = s.execute(
                text(
                    "SELECT a.atttypmod FROM pg_attribute a "
                    "WHERE a.attrelid = to_regclass('public.chunks') AND a.attname = 'embedding'"
                )
            ).scalar_one_or_none()
            result["chunks_table"] = {
                "exists": dim is not None,
                "embedding_dim": dim,
                "dim_matches_config": dim == expected_dim,
            }
            # No row count: under RLS (Phase 3) an anonymous health probe sees no rows, and the
            # index size is not something a public endpoint needs to reveal.
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001 - health must never raise
        result["error"] = type(exc).__name__
        result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    result["connect_stats"] = CONNECT_STATS.snapshot()
    return result
