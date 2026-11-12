"""Phase 6 benchmarks (self-service). Results go to eval/results/ and docs/results.json.

    python -m scripts.benchmark_phase6 upload    # upload -> ready -> first retrieval latency
    python -m scripts.benchmark_phase6 tenants   # 10 tenants, several thousand chunks, a tiny tenant

Both run on a throwaway Neon branch of ``dev`` (deleted afterwards), never on production.

- ``upload`` starts a real API process (real bge-small embedder, fake LLM) as a throwaway
  ``rag_app`` login on the branch. It registers a company and uploads small / medium / large
  files one at a time. For each file it measures (a) POST -> status ``ready``, polling every
  100 ms, and (b) ``ready`` -> the first /ask whose retrieved list contains the document.
- ``tenants`` loads 10 synthetic tenants (one tiny: 40 chunks; nine large). It checks that the
  pre-filter + HNSW iterative scan still returns a full top-k for the tiny tenant, and
  re-measures the filter overhead, including the Phase 6 ``status = 'ready'`` EXISTS.
No connection string is printed.
"""

from __future__ import annotations

import argparse
import io
import os
import random
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import and_, insert, text, true
from sqlalchemy.orm import Session

from app.admin.roles import app_url, new_password, upsert_login_role
from app.auth.policy import AuthUser
from app.db.models import Chunk, Document, Tenant
from app.retrieval.acl_filter import build_acl_clause
from scripts.benchmark import (
    WORDS,
    _engine,
    _recall,
    app_role_engine,
    record,
    stats,
    time_configs,
)

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- upload latency
def _sentences(rng: random.Random, n_chars: int, marker: str) -> str:
    out, size = [f"Benchmark document {marker}."], 0
    while size < n_chars:
        s = " ".join(rng.choice(WORDS) for _ in range(rng.randint(8, 16))).capitalize() + "."
        out.append(s)
        size += len(s) + 1
    return " ".join(out)


def _make_file(kind: str, body: str) -> tuple[str, bytes, str]:
    from tests import upload_files as uf

    if kind == "pdf":
        from pypdf import PdfReader, PdfWriter

        w = PdfWriter()
        for i in range(0, len(body), 3000):  # ~3 KB of text per page
            page = uf.text_pdf([body[j : j + 90] for j in range(i, min(i + 3000, len(body)), 90)])
            w.add_page(PdfReader(io.BytesIO(page)).pages[0])
        buf = io.BytesIO()
        w.write(buf)
        return ("large.pdf", buf.getvalue(), "application/pdf")
    if kind == "docx":
        return ("medium.docx", uf.docx([body[i : i + 600] for i in range(0, len(body), 600)]),
                "application/octet-stream")  # fmt: skip
    return ("small.txt", body.encode(), "text/plain")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_api(env: dict, log: Path) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    f = log.open("wb")
    proc = subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT,
    )  # fmt: skip
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 240
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("API exited: " + log.read_text(errors="replace")[-1500:])
        try:
            if httpx.get(url + "/health", timeout=5).status_code == 200:
                return proc, url
        except httpx.HTTPError:
            pass
        time.sleep(1)
    proc.terminate()
    raise RuntimeError("API did not start")


def cmd_upload(args: argparse.Namespace) -> None:
    from scripts.neon_branch import endpoint_info, throwaway_branch

    rng = random.Random(11)  # noqa: S311 - reproducible synthetic text
    sizes = {"small_txt_2kb": ("txt", 2_000, args.n_small), "medium_docx_60kb": ("docx", 60_000, args.n_medium),
             "large_pdf_400kb": ("pdf", 400_000, args.n_large)}  # fmt: skip
    with throwaway_branch("bench-upload") as br:
        role, pw = "rag_app_b_" + secrets.token_hex(4), new_password()
        owner = _engine(br.direct_url, 1)
        with Session(owner) as s, s.begin():
            upsert_login_role(s, role, pw)
        env = {**os.environ, "DATABASE_URL": app_url(br.pooled_url, role, pw),
               "DATABASE_URL_DIRECT": br.direct_url, "LLM_PROVIDER": "fake",
               "REGISTRATION_ENABLED": "true", "REGISTRATION_CODE": "", "UPLOAD_RATE_PER_MINUTE": "100000",
               "REGISTER_RATE_PER_HOUR": "100000", "LOGIN_RATE_PER_MINUTE": "100000"}  # fmt: skip
        log = Path(tempfile.gettempdir()) / "bench_upload_api.log"
        proc, url = _start_api(env, log)
        out: dict[str, dict] = {}
        try:
            c = httpx.Client(base_url=url, timeout=300)
            slug = "bench-up-" + secrets.token_hex(3)
            admin_pw = new_password()  # throwaway, kept in memory only
            reg = c.post("/tenants/register", json={"company_name": "Bench Co", "slug": slug,
                         "admin_email": f"admin@{slug}.example", "admin_password": admin_pw})  # fmt: skip
            reg.raise_for_status()
            tok = {"t": reg.json()["access_token"], "at": time.time()}

            def auth() -> dict[str, str]:
                # tokens live 15 min and the large-file run takes longer: re-login every 10 min
                if time.time() - tok["at"] > 600:
                    r = c.post("/auth/login", json={"email": f"admin@{slug}.example",
                                                    "password": admin_pw})  # fmt: skip
                    r.raise_for_status()
                    tok.update(t=r.json()["access_token"], at=time.time())
                return {"Authorization": f"Bearer {tok['t']}"}

            for label, (kind, n_chars, n) in sizes.items():
                to_ready, to_retrieval, chunks, failed = [], [], [], 0
                for i in range(n):
                    marker = f"ZXQ{label[:3].upper()}{i:02d}{secrets.token_hex(2).upper()}"
                    body = _sentences(rng, n_chars, marker)
                    f = _make_file(kind, body)
                    t0 = time.perf_counter()
                    r = c.post("/documents/upload", headers=auth(), files={"file": f},
                               data={"title": f"Bench {label} {i}", "classification": "internal"})  # fmt: skip
                    r.raise_for_status()
                    doc = r.json()["doc_id"]
                    while True:
                        st = c.get(f"/documents/{doc}/status", headers=auth()).json()
                        if st["status"] != "processing":
                            break
                        time.sleep(0.1)
                    t_ready = time.perf_counter()
                    if st["status"] != "ready":
                        failed += 1
                        continue
                    to_ready.append((t_ready - t0) * 1000)
                    chunks.append(st["chunk_count"])
                    q = {"question": f"What does benchmark document {marker} say?"}
                    for _ in range(20):
                        got = c.post("/ask", headers=auth(), json=q).json()
                        if doc in {x["doc_id"] for x in got.get("retrieved", [])}:
                            to_retrieval.append((time.perf_counter() - t_ready) * 1000)
                            break
                out[label] = {"files": n, "failed": failed, "bytes_each": len(f[1]),
                              "chunks_each": stats([float(x) for x in chunks]),
                              "upload_to_ready_ms": stats(to_ready),
                              "ready_to_first_retrieval_ms": stats(to_retrieval)}  # fmt: skip
                print(label, out[label])
            c.close()
        finally:
            proc.terminate()
            proc.wait(timeout=30)
            with Session(owner) as s, s.begin():
                s.execute(text(f"DROP ROLE IF EXISTS {role}"))
            owner.dispose()
        envinfo = endpoint_info(br.endpoint_id)
    record("upload_latency", {
        "environment": envinfo | {"api": "local uvicorn (Windows laptop) -> Neon over WAN",
                                  "embedding": "BAAI/bge-small-en-v1.5 on CPU", "llm": "fake",
                                  "poll_interval_ms": 100},
        "by_size": out,
        "method": "sequential uploads; client-side wall time; ready->retrieval = time from the "
                  "status poll that saw 'ready' to the first /ask that retrieved the document",
    })  # fmt: skip


# --------------------------------------------------------------------------- 10 tenants
def cmd_tenants(args: argparse.Namespace) -> None:
    from app.ingestion.embedder import HashEmbedder
    from scripts.neon_branch import endpoint_info, throwaway_branch

    rng = random.Random(13)  # noqa: S311
    emb = HashEmbedder(384)
    tenants = [uuid.uuid5(uuid.NAMESPACE_URL, f"bench-tenant-{i}") for i in range(args.tenants)]
    docs, chunks = [], []
    tiny = tenants[0]
    for ti, tid in enumerate(tenants):
        n_docs = 4 if ti == 0 else args.docs_per_tenant
        for i in range(n_docs):
            r = rng.random()
            cls = (
                "internal"
                if ti == 0
                else ("public" if r < 0.15 else "internal" if r < 0.6 else "confidential")
            )
            roles = (
                sorted(rng.sample(["hr", "finance", "engineering", "manager"], 2))
                if cls == "confidential"
                else []
            )
            did = f"t{ti}-doc-{i:04d}"
            docs.append({"id": did, "tenant_id": tid, "title": f"T{ti} doc {i}", "source_path": "synthetic",
                         "classification": cls, "allowed_roles": roles, "allowed_users": [],
                         "acl_version": 1, "status": "ready", "source": "seed"})  # fmt: skip
            topic = rng.sample(WORDS, 6)
            for j in range(10):
                body = " ".join(
                    rng.choice(topic if rng.random() < 0.6 else WORDS) for _ in range(60)
                )
                chunks.append({"chunk_id": uuid.uuid5(uuid.NAMESPACE_URL, f"{did}#{j}"), "doc_id": did,
                               "tenant_id": tid, "title": f"T{ti} doc {i}", "chunk_index": j, "text": body,
                               "embedding": emb.embed_documents([body])[0], "classification": cls,
                               "allowed_roles": roles, "allowed_users": [], "acl_version": 1})  # fmt: skip

    def persona(tid: uuid.UUID) -> AuthUser:
        return AuthUser(
            id=uuid.uuid4(),
            tenant_id=tid,
            roles=frozenset({"employee", rng.choice(["hr", "finance"])}),
        )

    def queries(pool: list[dict], n: int) -> list[list[float]]:
        out = []
        for _ in range(n):
            w = rng.choice(pool)["text"].split()
            rng.shuffle(w)
            out.append(emb.embed_query(" ".join(w[:15])))
        return out

    tiny_chunks = [c for c in chunks if c["tenant_id"] == tiny]
    n = args.n + args.warmup
    tiny_items = [
        (q, persona(tiny)) for q in queries(chunks, n)
    ]  # questions from ANY tenant's text
    big_items = [(q, persona(rng.choice(tenants[1:]))) for q in queries(chunks, n)]

    with throwaway_branch("bench-tenants") as br:
        owner = _engine(br.direct_url, 4)
        with Session(owner) as s, s.begin():
            s.execute(text("DELETE FROM chunks"))
            s.execute(text("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw"))
            s.execute(insert(Tenant), [{"id": t, "slug": f"bench-t{i}", "name": f"Bench T{i}",
                                        "source": "seed"} for i, t in enumerate(tenants)])  # fmt: skip
            for b in range(0, len(docs), 1000):
                s.execute(insert(Document), docs[b : b + 1000])
        with Session(owner) as s, s.begin():
            for b in range(0, len(chunks), 1000):
                s.execute(insert(Chunk), chunks[b : b + 1000])
        with Session(owner) as s, s.begin():
            s.execute(text("SET LOCAL maintenance_work_mem = '256MB'"))
            s.execute(text("CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw "
                           "(embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"))  # fmt: skip
            s.execute(text("ANALYZE chunks"))
            s.execute(text("ANALYZE documents"))
            total = s.execute(text("SELECT count(*) FROM chunks")).scalar_one()
        pooled_owner = _engine(br.pooled_url, 4)

        def no_ready(u):  # type: ignore[no-untyped-def]
            return and_(*list(build_acl_clause(u).clauses)[:-1])

        with app_role_engine(br.direct_url, br.pooled_url, 4) as app:
            results = {}
            for label, items in (
                ("tiny_tenant_40_chunks", tiny_items),
                ("large_tenants", big_items),
            ):
                cfg = {
                    "no_filter": (pooled_owner, lambda u: true(), False, 5),
                    "where_only": (pooled_owner, build_acl_clause, False, 5),
                    "where_only_without_ready_exists": (pooled_owner, no_ready, False, 5),
                    "where_and_rls": (app, build_acl_clause, True, 5),
                }
                timing = time_configs(cfg, items, warmup=args.warmup, explain=args.explain)
                timing |= time_configs({"where_and_rls_iterative_off": cfg["where_and_rls"]}, items,
                                       warmup=args.warmup, mode="off", ef=40, explain=args.explain)  # fmt: skip
                nf, wr = timing["no_filter"], timing["where_and_rls"]
                wo, wn = timing["where_only"], timing["where_only_without_ready_exists"]
                results[label] = {
                    "configs": timing,
                    "overhead_where_and_rls_vs_no_filter_ms": {
                        "client_p50": round(wr["client_ms"]["p50"] - nf["client_ms"]["p50"], 2),
                        "client_p95": round(wr["client_ms"]["p95"] - nf["client_ms"]["p95"], 2),
                        "server_p50": round(wr["server_exec_ms"]["p50"] - nf["server_exec_ms"]["p50"], 3),
                        "server_p95": round(wr["server_exec_ms"]["p95"] - nf["server_exec_ms"]["p95"], 3),
                    },
                    "ready_exists_cost_server_ms": {
                        "p50": round(wo["server_exec_ms"]["p50"] - wn["server_exec_ms"]["p50"], 3),
                        "p95": round(wo["server_exec_ms"]["p95"] - wn["server_exec_ms"]["p95"], 3),
                    },
                    "recall_at_5": _recall(app, items[args.warmup : args.warmup + 100], k=5),
                }  # fmt: skip
        pooled_owner.dispose()
        owner.dispose()
        envinfo = endpoint_info(br.endpoint_id)
    record("tenants_scale", {
        "environment": envinfo | {"embedding": "hash-384 (synthetic corpus)", "top_k": 5},
        "corpus": {"tenants": args.tenants, "chunks": int(total), "tiny_tenant_chunks": len(tiny_chunks),
                   "other_tenant_chunks_each": args.docs_per_tenant * 10},
        "queries_timed_per_config": args.n, "results": results,
        "note": "tiny-tenant queries are drawn from ALL tenants' text, so the nearest neighbours "
                "are mostly other tenants' chunks: the hardest case for a filtered HNSW scan",
    })  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    import logging

    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("upload")
    p.add_argument("--n-small", type=int, default=10)
    p.add_argument("--n-medium", type=int, default=10)
    p.add_argument("--n-large", type=int, default=5)
    p = sub.add_parser("tenants")
    p.add_argument("--tenants", type=int, default=10)
    p.add_argument("--docs-per-tenant", type=int, default=70)
    p.add_argument("--n", type=int, default=300)
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--explain", type=int, default=100)
    args = ap.parse_args(argv)
    {"upload": cmd_upload, "tenants": cmd_tenants}[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
