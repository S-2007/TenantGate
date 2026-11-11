"""(Re)build the chunk index in Neon idempotently.

python -m scripts.ingest            # upsert all manifest docs
python -m scripts.ingest --prune    # ...and delete chunks of docs no longer in the manifest
python -m scripts.ingest --dry-run  # chunk + report sizes, no embedding, no DB

Runs as the OWNER role over DATABASE_URL_DIRECT (an admin job, like migrations). Each chunk's ACL
is read from the documents table, so run ``python -m scripts.seed`` first. Any problem (missing
document row, tenant mismatch, invalid chunk) aborts the whole run with exit code 1. Nothing is
written, nothing is skipped silently.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from dataclasses import asdict

from app.config import get_settings
from app.db.session import admin_transaction, reset_admin_engine
from app.ingestion.corpus import load_documents
from app.ingestion.embedder import get_embedder
from app.ingestion.pipeline import IngestError, build_chunks, count_chunks, ingest

# Rough per-row footprint: 384 float4 (1.5 KB) + text (~2.5 KB) + index overhead.
APPROX_BYTES_PER_CHUNK = 6_000
NEON_FREE_STORAGE_BYTES = 500 * 1024 * 1024


def _quiet_logs() -> None:
    import logging as _l

    for name in ("httpx", "httpx2", "huggingface_hub", "sentence_transformers"):
        _l.getLogger(name).setLevel(_l.WARNING)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    _quiet_logs()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tenant", default=None)
    args = ap.parse_args(argv)

    s = get_settings()
    docs = load_documents(s.corpus_path, tenant=args.tenant)
    embedder = get_embedder(s.embedding_model, s.embedding_dim)
    if args.prune and args.tenant:
        ap.error("--prune with --tenant would delete other tenants' chunks")

    if args.dry_run:
        chunks = build_chunks(docs, embedder, size=s.chunk_size, overlap=s.chunk_overlap)
        toks = [c.n_tokens for c in chunks]
        print(
            json.dumps(
                {
                    "documents": len(docs),
                    "chunks": len(chunks),
                    "tokens_per_chunk": {
                        "min": min(toks),
                        "median": statistics.median(toks),
                        "max": max(toks),
                    },
                    "approx_storage_mb": round(len(chunks) * APPROX_BYTES_PER_CHUNK / 1e6, 2),
                },
                indent=2,
            )
        )
        return 0

    s.validate_for_migrations()
    est = len(docs) * 4 * APPROX_BYTES_PER_CHUNK
    if est > NEON_FREE_STORAGE_BYTES * 0.5:
        print("Refusing: estimated storage exceeds half of the Neon free tier", file=sys.stderr)
        return 2
    try:
        with admin_transaction() as session:
            stats = ingest(
                session,
                docs,
                embedder,
                size=s.chunk_size,
                overlap=s.chunk_overlap,
                prune=args.prune,
            )
            total = count_chunks(session)
    except IngestError as exc:
        logging.getLogger("scripts.ingest").error("ingest aborted, nothing written: %s", exc)
        return 1
    finally:
        reset_admin_engine()
    print(json.dumps({**asdict(stats), "total_chunks_in_db": total}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
