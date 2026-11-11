"""Latency benchmark against Neon (dev branch by default).

Measures: physical connect time of a fresh process (cold if the compute was suspended),
SELECT 1 round trip, and end-to-end retrieval (embed + vector search) over eval questions.
Prints JSON and writes eval/results/<ts>_benchmark.json. Never prints connection strings.

    python -m scripts.benchmark --n 30
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from app.config import get_settings
from app.db.session import CONNECT_STATS, transaction
from app.ingestion.embedder import get_embedder
from app.retrieval.retriever import search


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))], 1)


def _quiet_logs() -> None:
    import logging as _l

    for name in ("httpx", "httpx2", "huggingface_hub", "sentence_transformers"):
        _l.getLogger(name).setLevel(_l.WARNING)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    _quiet_logs()
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30, help="number of retrieval queries")
    ap.add_argument("--eval-set", default="data/eval/eval_set.jsonl")
    args = ap.parse_args(argv)
    s = get_settings()
    s.require("database_url")

    t0 = time.perf_counter()
    with transaction() as db:
        db.execute(text("SELECT 1"))
    first_query_ms = (time.perf_counter() - t0) * 1000

    pings = []
    for _ in range(10):
        t = time.perf_counter()
        with transaction() as db:
            db.execute(text("SELECT 1"))
        pings.append((time.perf_counter() - t) * 1000)

    emb = get_embedder(s.embedding_model, s.embedding_dim)
    qs = [
        json.loads(line)["question"]
        for line in Path(args.eval_set).read_text().splitlines()
        if line.strip()
    ][: args.n]
    embed_ms, search_ms = [], []
    for q in qs:
        t = time.perf_counter()
        v = emb.embed_query(q)
        t1 = time.perf_counter()
        with transaction() as db:
            search(db, v, s.top_k)
        embed_ms.append((t1 - t) * 1000)
        search_ms.append((time.perf_counter() - t1) * 1000)

    out = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "first_connect_ms": CONNECT_STATS.snapshot()["first_connect_ms"],
        "first_query_incl_connect_ms": round(first_query_ms, 1),
        "connect_retries": CONNECT_STATS.snapshot()["retries"],
        "select1_ms": {"p50": _pct(pings, 0.5), "p95": _pct(pings, 0.95)},
        "embed_query_ms": {"p50": _pct(embed_ms, 0.5), "p95": _pct(embed_ms, 0.95)},
        "vector_search_ms": {
            "p50": _pct(search_ms, 0.5),
            "p95": _pct(search_ms, 0.95),
            "mean": round(statistics.mean(search_ms), 1),
        },
        "n_queries": len(qs),
        "embedding_model": s.embedding_model,
        "top_k": s.top_k,
    }
    print(json.dumps(out, indent=2))
    res = Path("eval/results")
    res.mkdir(parents=True, exist_ok=True)
    (res / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_benchmark.json").write_text(
        json.dumps(out, indent=2)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
