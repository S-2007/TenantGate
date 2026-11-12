"""Phase 5 benchmarks. Every number goes to eval/results/<ts>_bench_<name>.json and docs/results.json.

    python -m scripts.benchmark latency   # ACL filter overhead (>= 500 queries), post-filter, RLS
                                          # on/off, drop rates; on a throwaway branch of dev
    python -m scripts.benchmark scale     # ~20k synthetic chunks: overhead, index impact, recall@5
    python -m scripts.benchmark neon      # cold start after suspend, pooled vs direct, retries
    python -m scripts.benchmark denied    # every eval question as UNAUTHORIZED personas (dev)
    python -m scripts.benchmark all

Method:
- Anything that changes the database or its compute runs on a throwaway Neon branch created
  from ``dev``. The branch is always deleted afterwards.
- Every configuration has its own connection and thread, and runs the SAME query list. Identity
  and HNSW settings are bound before each query (``set_config(..., true)``), and only the
  search statement itself is timed.
- Client times include the WAN round trip to Neon. ``EXPLAIN (ANALYZE)`` times are server-side
  execution only, which isolates what the filter really costs.
No connection string is ever printed.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import secrets
import statistics
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import ColumnElement, Engine, select, text, true
from sqlalchemy.orm import Session

from app.admin.roles import app_url, new_password, upsert_login_role
from app.auth.policy import AuthUser, DocACL, can_access
from app.config import get_settings, normalize_pg_url
from app.db.models import Chunk
from app.db.rls import apply_rls_context
from app.db.session import build_engine, connect_with_retry
from app.retrieval.acl_filter import build_acl_clause
from app.retrieval.retriever import configure_hnsw

RESULTS_DIR = Path("eval/results")
RESULTS_JSON = Path("docs/results.json")
log = logging.getLogger("benchmark")


# --------------------------------------------------------------------------- helpers
def stats(xs: list[float]) -> dict:
    """p50 / p95 / mean (ms, 2 decimals) and n."""
    if not xs:
        return {"n": 0}
    s = sorted(xs)
    p = lambda q: s[min(len(s) - 1, int(round(q * (len(s) - 1))))]  # noqa: E731
    return {"n": len(s), "p50": round(p(0.5), 2), "p95": round(p(0.95), 2),
            "mean": round(statistics.mean(s), 2)}  # fmt: skip


def record(section: str, data: dict) -> None:
    """Write the section's JSON and merge it into docs/results.json."""
    data = {"timestamp": datetime.now(UTC).isoformat(timespec="seconds"), **data}
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}_bench_{section}.json").write_text(
        json.dumps(data, indent=2, default=str), encoding="utf-8"
    )
    agg = json.loads(RESULTS_JSON.read_text(encoding="utf-8")) if RESULTS_JSON.exists() else {}
    agg[section] = data
    RESULTS_JSON.write_text(json.dumps(agg, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(data, indent=2, default=str))


def _engine(url: str, pool: int = 2) -> Engine:
    return build_engine(normalize_pg_url(url, var_name="BENCH_URL"), pool_size=pool, max_overflow=1)


@contextmanager
def app_role_engine(owner_direct: str, pooled_owner_url: str, pool: int = 2) -> Iterator[Engine]:
    """A throwaway rag_app login on the benchmark branch (the production access path)."""
    name, pw = "rag_app_b_" + secrets.token_hex(4), new_password()
    owner = _engine(owner_direct, 1)
    with Session(owner) as s, s.begin():
        upsert_login_role(s, name, pw)
    eng = _engine(app_url(pooled_owner_url, name, pw), pool)
    try:
        yield eng
    finally:
        eng.dispose()
        with Session(owner) as s, s.begin():
            s.execute(text(f"DROP ROLE IF EXISTS {name}"))
        owner.dispose()


def _bind(s: Session, user: AuthUser | None, *, ef: int, mode: str) -> None:
    if user is not None:
        apply_rls_context(s, user)
    configure_hnsw(s, ef_search=ef, iterative_scan=mode)


def _stmt(qvec, k: int, clause: ColumnElement[bool]):  # type: ignore[no-untyped-def]
    dist = Chunk.embedding.cosine_distance(list(qvec)).label("d")
    return (
        select(Chunk.chunk_id, Chunk.doc_id, Chunk.tenant_id, Chunk.classification,
               Chunk.allowed_roles, Chunk.allowed_users, dist)
        .where(clause).order_by(dist).limit(k)
    )  # fmt: skip


def _explain(s: Session, stmt) -> tuple[float, bool]:  # type: ignore[no-untyped-def]
    """Server-side execution time (ms) of ``stmt`` and whether the HNSW index was used."""
    c = stmt.compile(dialect=s.bind.dialect)
    params = {
        k: ("[" + ",".join(f"{x:.7g}" for x in v) + "]")
        if isinstance(v, list) and v and isinstance(v[0], float)
        else v
        for k, v in c.params.items()
    }
    raw = (
        s.connection().exec_driver_sql("EXPLAIN (ANALYZE, FORMAT JSON) " + str(c), params).scalar()
    )
    plan = (raw if isinstance(raw, list) else json.loads(raw))[0]
    return float(plan["Execution Time"]), "ix_chunks_embedding_hnsw" in json.dumps(plan)


def _acl_of(r) -> DocACL:  # type: ignore[no-untyped-def]
    return DocACL(r.doc_id, r.tenant_id, r.classification, frozenset(r.allowed_roles),
                  frozenset(r.allowed_users))  # fmt: skip


def eval_queries() -> list[tuple[str, AuthUser]]:
    """(question, persona AuthUser) for every eval row, identities from the seed plan."""
    from eval.run_eval import manifest_identity

    _, resolve = manifest_identity()
    rows = [
        json.loads(x) for x in Path("data/eval/eval_set.jsonl").read_text("utf-8").splitlines() if x
    ]
    return [(r["question"], resolve(r["tenant"], r["persona"])) for r in rows]


Config = tuple[
    Engine, Callable[[AuthUser], ColumnElement[bool]], bool, int
]  # engine, clause, rls, k


def time_configs(configs: dict[str, Config], items: list[tuple[list[float], AuthUser]], *,
                 warmup: int, ef: int = 100, mode: str = "strict_order",
                 explain: int = 0) -> dict:  # fmt: skip
    """Run every config over ``items`` (each config on its own thread/connection)."""

    def one(name: str) -> dict:
        eng, clause_fn, rls, k = configs[name]
        wall, server, hnsw, short, rows_back = [], [], 0, 0, 0
        with Session(eng) as s:
            for i, (qvec, user) in enumerate(items):
                if i % 50 == 0 and s.in_transaction():
                    s.commit()
                _bind(s, user if rls else None, ef=ef, mode=mode)
                stmt = _stmt(qvec, k, clause_fn(user))
                t = time.perf_counter()
                rows = s.execute(stmt).all()
                dt = (time.perf_counter() - t) * 1000
                if i >= warmup:
                    wall.append(dt)
                    rows_back += len(rows)
                    if len(rows) < min(k, 5):
                        short += 1
                if warmup <= i < warmup + explain:
                    ms, used = _explain(s, stmt)
                    server.append(ms)
                    hnsw += used
            s.commit()
        n = len(wall)
        return {"client_ms": stats(wall), "server_exec_ms": stats(server),
                "hnsw_plan_share": round(hnsw / max(1, len(server)), 3),
                "queries_short_of_k": short, "avg_rows": round(rows_back / max(1, n), 2)}  # fmt: skip

    with ThreadPoolExecutor(max_workers=len(configs)) as pool:
        futs = {name: pool.submit(one, name) for name in configs}
        return {name: f.result() for name, f in futs.items()}


def post_filter_shortfall(eng: Engine, items, *, fetch: int, k: int = 5) -> dict:  # type: ignore[no-untyped-def]
    """Retrieve-then-drop: unfiltered top-``fetch``, then can_access; how often < k survive."""
    short, survivors = 0, []
    with Session(eng) as s:
        configure_hnsw(s, ef_search=100, iterative_scan="strict_order")
        for qvec, user in items:
            rows = s.execute(_stmt(qvec, fetch, true())).all()
            kept = [r for r in rows if can_access(user, _acl_of(r))][:k]
            survivors.append(len(kept))
            short += len(kept) < k
    return {"fetch": fetch, "k": k, "queries": len(items), "queries_short_of_k": short,
            "share_short": round(short / max(1, len(items)), 3),
            "avg_survivors": round(statistics.mean(survivors), 2)}  # fmt: skip


# --------------------------------------------------------------------------- latency
def cmd_latency(args: argparse.Namespace) -> None:
    from app.generation.llm import FakeLLM
    from app.ingestion.embedder import get_embedder
    from app.retrieval.service import RAGService
    from scripts.neon_branch import endpoint_info, throwaway_branch

    s = get_settings()
    emb = get_embedder(s.embedding_model, s.embedding_dim)
    base = eval_queries()
    vecs = [emb.embed_query(q) for q, _ in base]
    n_total = args.n + args.warmup
    items = [(vecs[i % len(base)], base[i % len(base)][1]) for i in range(n_total)]
    with throwaway_branch("bench-lat") as br:
        owner = _engine(br.pooled_url, 4)
        with app_role_engine(br.direct_url, br.pooled_url, 4) as app:
            configs: dict[str, Config] = {
                "no_filter": (owner, lambda u: true(), False, 5),
                "where_only": (owner, build_acl_clause, False, 5),
                "rls_only": (app, lambda u: true(), True, 5),
                "where_and_rls": (app, build_acl_clause, True, 5),
            }
            timing = time_configs(configs, items, warmup=args.warmup, explain=args.explain)
            post = post_filter_shortfall(
                owner, items[args.warmup : args.warmup + len(base)], fetch=10
            )

            @contextmanager
            def app_tx():  # type: ignore[no-untyped-def]
                with Session(app) as ss, ss.begin():
                    yield ss

            @contextmanager
            def owner_tx():  # type: ignore[no-untyped-def]
                with Session(owner) as ss, ss.begin():
                    yield ss

            def drop_run(svc: RAGService) -> dict:
                retrieved = dropped = 0
                lat = []
                for q, u in base:
                    ok, dr, tm = svc.retrieve(q, u)
                    retrieved += len(ok) + len(dr)
                    dropped += len(dr)
                    lat.append(tm["search"] + tm["verify"])
                return {"queries": len(base), "chunks_reaching_check": retrieved, "dropped": dropped,
                        "drop_rate": round(dropped / max(1, retrieved), 4),
                        "retrieve_db_ms": stats(lat)}  # fmt: skip

            normal = drop_run(
                RAGService(embedder=emb, llm=FakeLLM(), top_k=5, session_factory=app_tx)
            )
            fault = drop_run(RAGService(embedder=emb, llm=FakeLLM(), top_k=5, session_factory=owner_tx,
                                        acl_clause_fn=lambda u: true(), rls_fn=lambda x, u: True))  # fmt: skip
        owner.dispose()
        env = endpoint_info(br.endpoint_id)
    nf, wr = timing["no_filter"], timing["where_and_rls"]
    record("latency", {
        "environment": env | {"chunks": 184, "embedding_model": s.embedding_model, "top_k": 5,
                              "hnsw": "ef_search=100, iterative_scan=strict_order",
                              "client": "developer laptop over WAN to Neon"},  # fmt: skip
        "queries_timed_per_config": args.n, "warmup": args.warmup, "explain_per_config": args.explain,
        "configs": timing,
        "overhead_where_and_rls_vs_no_filter_ms": {
            "client_p50": round(wr["client_ms"]["p50"] - nf["client_ms"]["p50"], 2),
            "client_p95": round(wr["client_ms"]["p95"] - nf["client_ms"]["p95"], 2),
            "server_p50": round(wr["server_exec_ms"]["p50"] - nf["server_exec_ms"]["p50"], 3),
            "server_p95": round(wr["server_exec_ms"]["p95"] - nf["server_exec_ms"]["p95"], 3),
        },
        "post_filter": post,
        "second_check_normal": normal,
        "second_check_fault_injection": fault,
    })  # fmt: skip


# --------------------------------------------------------------------------- scale
WORDS = ("alpha bravo cobalt delta ember fjord granite harbor ion jade krypton lumen meadow "
         "nickel onyx prism quartz raven saffron tundra umber violet willow xenon yarrow zephyr "
         "audit budget compliance deploy forecast hiring incident invoice ledger merger payroll "
         "pipeline policy quota rollout runbook salary security staffing vendor").split()  # fmt: skip


def _synth_text(rng: random.Random, topic: list[str]) -> str:
    return " ".join(rng.choice(topic if rng.random() < 0.6 else WORDS) for _ in range(60))


def cmd_scale(args: argparse.Namespace) -> None:
    from sqlalchemy import insert

    from app.admin.seed import build_plan
    from app.db.models import Document
    from app.ingestion.corpus import load_manifest
    from app.ingestion.embedder import HashEmbedder
    from scripts.neon_branch import endpoint_info, throwaway_branch

    rng = random.Random(7)  # noqa: S311 - reproducible synthetic data, not security
    emb = HashEmbedder(384)
    plan = build_plan(load_manifest("data/corpus"))
    users_by_tenant: dict[uuid.UUID, list] = {}
    for u in plan.users:
        users_by_tenant.setdefault(u.tenant_id, []).append(u)
    tenants = [t.id for t in plan.tenants]
    per_doc = 10
    n_docs = args.chunks // per_doc
    docs, chunks = [], []
    for i in range(n_docs):
        tid = tenants[i % len(tenants)]
        r = rng.random()
        cls = "public" if r < 0.15 else "internal" if r < 0.6 else "confidential"
        roles: list[str] = []
        users: list[uuid.UUID] = []
        if cls == "confidential":
            if rng.random() < 0.1:
                users = [rng.choice(users_by_tenant[tid]).id]
            else:
                roles = sorted(
                    set(rng.sample(["admin", "hr", "finance", "engineering", "manager"], 2))
                )
        did = f"synth-{i:05d}"
        docs.append({"id": did, "tenant_id": tid, "title": f"Synthetic {i}", "source_path": "synthetic",
                     "classification": cls, "allowed_roles": roles, "allowed_users": users,
                     "acl_version": 1})  # fmt: skip
        topic = rng.sample(WORDS, 6)
        for j in range(per_doc):
            body = _synth_text(rng, topic)
            chunks.append({"chunk_id": uuid.uuid5(uuid.NAMESPACE_URL, f"{did}#{j}"), "doc_id": did,
                           "tenant_id": tid, "title": f"Synthetic {i}", "chunk_index": j, "text": body,
                           "embedding": emb.embed_documents([body])[0], "classification": cls,
                           "allowed_roles": roles, "allowed_users": users, "acl_version": 1})  # fmt: skip
    queries = []
    for _ in range(args.n + args.warmup):
        c = rng.choice(chunks)
        words = c["text"].split()
        rng.shuffle(words)
        queries.append(emb.embed_query(" ".join(words[:15])))
    active = [u for u in plan.users if u.is_active]
    personas = [AuthUser(id=u.id, tenant_id=u.tenant_id, roles=frozenset(u.roles)) for u in active]
    items = [(queries[i], personas[i % len(personas)]) for i in range(len(queries))]

    with throwaway_branch("bench-scale") as br:
        owner = _engine(br.direct_url, 4)
        t0 = time.perf_counter()
        with Session(owner) as s, s.begin():
            s.execute(text("DELETE FROM chunks"))
            s.execute(text("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw"))
            for b in range(0, len(docs), 1000):
                s.execute(insert(Document), docs[b : b + 1000])
        with Session(owner) as s, s.begin():
            for b in range(0, len(chunks), 1000):
                s.execute(insert(Chunk), chunks[b : b + 1000])
        load_s = time.perf_counter() - t0
        t0 = time.perf_counter()
        with Session(owner) as s, s.begin():
            s.execute(text("SET LOCAL maintenance_work_mem = '256MB'"))
            s.execute(text("CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw "
                           "(embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"))  # fmt: skip
            s.execute(text("ANALYZE chunks"))
        index_s = time.perf_counter() - t0
        with Session(owner) as s:
            size = s.execute(text("SELECT pg_size_pretty(pg_total_relation_size('chunks')), "
                                  "pg_size_pretty(pg_database_size(current_database())), "
                                  "(SELECT count(*) FROM chunks)")).one()  # fmt: skip
        pooled_owner = _engine(br.pooled_url, 4)
        with app_role_engine(br.direct_url, br.pooled_url, 4) as app:
            configs: dict[str, Config] = {
                "no_filter": (pooled_owner, lambda u: true(), False, 5),
                "where_only": (pooled_owner, build_acl_clause, False, 5),
                "where_and_rls": (app, build_acl_clause, True, 5),
            }
            timing = time_configs(configs, items, warmup=args.warmup, explain=args.explain)
            no_iter = time_configs({"where_and_rls_iterative_off": configs["where_and_rls"]}, items,
                                   warmup=args.warmup, mode="off", explain=args.explain)  # fmt: skip
            timing |= no_iter
            recall = _recall(app, items[args.warmup :], k=5)
            post = post_filter_shortfall(
                pooled_owner, items[args.warmup : args.warmup + 100], fetch=10
            )
            noidx = _without_acl_indexes(owner, items, warmup=args.warmup, explain=args.explain)
        pooled_owner.dispose()
        owner.dispose()
        env = endpoint_info(br.endpoint_id)
    nf, wr = timing["no_filter"], timing["where_and_rls"]
    record("scale", {
        "environment": env | {"embedding": "hash-384 (synthetic corpus)", "top_k": 5},
        "corpus": {"chunks": int(size[2]), "documents": n_docs, "chunks_table_size": size[0],
                   "database_size": size[1], "load_seconds": round(load_s, 1),
                   "hnsw_build_seconds": round(index_s, 1),
                   "mix": "15% public, 45% internal, 40% confidential (10% of those user-specific)"},
        "queries_timed_per_config": args.n, "configs": timing,
        "overhead_where_and_rls_vs_no_filter_ms": {
            "client_p50": round(wr["client_ms"]["p50"] - nf["client_ms"]["p50"], 2),
            "client_p95": round(wr["client_ms"]["p95"] - nf["client_ms"]["p95"], 2),
            "server_p50": round(wr["server_exec_ms"]["p50"] - nf["server_exec_ms"]["p50"], 3),
            "server_p95": round(wr["server_exec_ms"]["p95"] - nf["server_exec_ms"]["p95"], 3),
        },
        "recall_at_5_with_filter": recall,
        "post_filter": post,
        "without_gin_and_tenant_btree": noidx,
    })  # fmt: skip


def _recall(app: Engine, items, *, k: int) -> dict:  # type: ignore[no-untyped-def]
    """HNSW (+filter) top-k vs exact filtered top-k (index scans disabled), averaged."""
    out = {}
    for label, ef, mode in (
        ("strict_order_ef100", 100, "strict_order"),
        ("iterative_off_ef40", 40, "off"),
    ):
        hits = total = 0
        with Session(app) as s:
            for i, (qvec, user) in enumerate(items):
                if i % 50 == 0 and s.in_transaction():
                    s.commit()
                _bind(s, user, ef=ef, mode=mode)
                approx = {r.chunk_id for r in s.execute(_stmt(qvec, k, build_acl_clause(user)))}
                s.execute(text("SET LOCAL enable_indexscan = off"))
                exact = {r.chunk_id for r in s.execute(_stmt(qvec, k, build_acl_clause(user)))}
                s.execute(text("SET LOCAL enable_indexscan = on"))
                hits += len(approx & exact)
                total += len(exact)
            s.commit()
        out[label] = {"queries": len(items), "recall": round(hits / max(1, total), 4)}
    return out


def _without_acl_indexes(owner: Engine, items, *, warmup: int, explain: int) -> dict:  # type: ignore[no-untyped-def]
    """where_only timing inside a transaction that dropped the GIN + tenant btree (rolled back)."""
    wall, server = [], []
    with Session(owner) as s:
        s.begin()
        for ix in ("ix_chunks_tenant_id", "ix_chunks_allowed_roles", "ix_chunks_allowed_users"):
            s.execute(text(f"DROP INDEX {ix}"))
        configure_hnsw(s, ef_search=100, iterative_scan="strict_order")
        for i, (qvec, user) in enumerate(items):
            stmt = _stmt(qvec, 5, build_acl_clause(user))
            t = time.perf_counter()
            s.execute(stmt).all()
            if i >= warmup:
                wall.append((time.perf_counter() - t) * 1000)
            if warmup <= i < warmup + explain:
                server.append(_explain(s, stmt)[0])
        s.rollback()  # indexes come back
    return {"where_only_client_ms": stats(wall), "where_only_server_exec_ms": stats(server)}


# --------------------------------------------------------------------------- neon
def cmd_neon(args: argparse.Namespace) -> None:
    from app.db.session import ConnectStats
    from scripts.neon_branch import endpoint_info, suspend_endpoint, throwaway_branch

    def dsn(url: str) -> str:
        return normalize_pg_url(url, var_name="BENCH_URL").replace(
            "postgresql+psycopg://", "postgresql://"
        )

    out: dict = {}
    with throwaway_branch("bench-neon") as br:
        urls = {"direct": dsn(br.direct_url), "pooled": dsn(br.pooled_url)}
        connect_with_retry(urls["direct"]).close()  # make sure the compute is up
        for kind, url in urls.items():
            conns, pings = [], []
            for _ in range(args.connects):
                t = time.perf_counter()
                c = connect_with_retry(url)
                conns.append((time.perf_counter() - t) * 1000)
                c.close()
            c = connect_with_retry(url)
            for _ in range(args.pings):
                t = time.perf_counter()
                c.execute("SELECT 1").fetchone()
                pings.append((time.perf_counter() - t) * 1000)
            c.close()
            out[kind] = {"new_connection_ms": stats(conns), "select1_warm_ms": stats(pings)}
        cold = []
        for trial in range(args.cold):
            kind = ("direct", "pooled")[trial % 2]
            suspend_s = suspend_endpoint(br.endpoint_id)
            st = ConnectStats()
            t = time.perf_counter()
            c = connect_with_retry(urls[kind], stats=st)
            c.execute("SELECT 1").fetchone()
            cold.append({"via": kind, "first_query_ms": round((time.perf_counter() - t) * 1000, 1),
                         "connect_retries": st.snapshot()["retries"],
                         "suspend_took_s": round(suspend_s, 1)})  # fmt: skip
            c.close()
        env = endpoint_info(br.endpoint_id)
    record("neon", {
        "environment": env, "connections": out, "cold_starts": cold,
        "cold_start_first_query_ms": stats([c["first_query_ms"] for c in cold]),
        "retry_backoff": {
            "retries_needed_in_cold_starts": sum(c["connect_retries"] for c in cold),
            "note": "connect_with_retry: 4 attempts, exponential backoff from 0.5 s, connect_timeout "
                    "10 s. Cold starts completed within the first attempt. Query-level: one retry on "
                    "OperationalError (D-035), which absorbed the pooler blip seen in Phase 4.",
        },
    })  # fmt: skip


# --------------------------------------------------------------------------- denied
def cmd_denied(args: argparse.Namespace) -> None:
    """Ask every eval question as personas who may NOT read its sources (dev branch, app role)."""
    from app.admin.seed import build_plan
    from app.db.session import transaction
    from app.generation.llm import FakeLLM
    from app.ingestion.corpus import load_manifest
    from app.ingestion.embedder import get_embedder
    from app.retrieval.service import RAGService

    s = get_settings()
    plan = build_plan(load_manifest("data/corpus"))
    acl = {d.id: DocACL(d.id, d.tenant_id, d.classification, frozenset(d.allowed_roles),
                        frozenset(d.allowed_users)) for d in plan.documents}  # fmt: skip
    users = [AuthUser(id=u.id, tenant_id=u.tenant_id, roles=frozenset(u.roles), is_active=u.is_active)
             for u in plan.users if u.is_active]  # fmt: skip
    tenant_of = {t.id: t.slug for t in plan.tenants}
    svc = RAGService(embedder=get_embedder(s.embedding_model, s.embedding_dim), llm=FakeLLM(),
                     top_k=s.top_k, session_factory=transaction)  # fmt: skip
    rows = [
        json.loads(x) for x in Path("data/eval/eval_set.jsonl").read_text("utf-8").splitlines() if x
    ]
    checks = violations = refused = same_tenant = 0
    for r in rows:
        srcs = r["source_doc_ids"]
        if not srcs:
            continue
        denied = [u for u in users if not any(can_access(u, acl[d]) for d in srcs)]
        same = [u for u in denied if tenant_of[u.tenant_id] == r["tenant"]]
        other = [u for u in denied if tenant_of[u.tenant_id] != r["tenant"]]
        for u in [*same[:1], *other[:1]]:
            res = svc.ask(r["question"], u)
            got = {c.doc_id for c in res.retrieved} | {c.doc_id for c in res.cited}
            checks += 1
            same_tenant += u in same
            violations += bool(got & set(srcs))
            refused += res.refused
    record("access_denied", {
        "eval_questions": len([r for r in rows if r["source_doc_ids"]]),
        "unauthorized_asks": checks, "of_which_same_tenant_persona": same_tenant,
        "answers_using_a_restricted_source": violations,
        "refusals": refused,
        "method": "service layer as the dev app role, fake LLM; a violation = any source doc of the "
                  "question retrieved or cited for a persona that may not read it",
    })  # fmt: skip


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    # the fault-injection drop run logs one SECURITY_ALERT per dropped chunk by design
    logging.getLogger("app.security").setLevel(logging.CRITICAL)
    for name in ("httpx", "huggingface_hub", "sentence_transformers"):
        logging.getLogger(name).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("latency")
    p.add_argument("--n", type=int, default=540)
    p.add_argument("--warmup", type=int, default=50)
    p.add_argument("--explain", type=int, default=150)
    p = sub.add_parser("scale")
    p.add_argument("--chunks", type=int, default=20000)
    p.add_argument("--n", type=int, default=300)
    p.add_argument("--warmup", type=int, default=30)
    p.add_argument("--explain", type=int, default=100)
    p = sub.add_parser("neon")
    p.add_argument("--connects", type=int, default=20)
    p.add_argument("--pings", type=int, default=100)
    p.add_argument("--cold", type=int, default=4)
    sub.add_parser("denied")
    sub.add_parser("all")
    args = ap.parse_args(argv)
    cmds = {"latency": cmd_latency, "scale": cmd_scale, "neon": cmd_neon, "denied": cmd_denied}
    if args.cmd == "all":
        for name in ("latency", "scale", "neon", "denied"):
            cmds[name](ap.parse_args([name]))
    else:
        cmds[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
