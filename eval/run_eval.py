"""Run the eval set through the RAG pipeline and score it.

    python -m eval.run_eval --label baseline_no_acl                  # service layer (default)
    python -m eval.run_eval --label x --mode http --base-url http://localhost:8000
    python -m eval.run_eval --label x --judge fake --limit 10
    python -m eval.run_eval --label x --mode memory   # exact in-memory search, no DB (offline)
    python -m eval.run_eval --compare eval/results/a.csv eval/results/b.csv

Writes eval/results/<UTC timestamp>_<label>.csv (one row per question) and a matching
.summary.json, then prints a summary table.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eval.judge import FakeJudge, Judge, LLMJudge, Verdict
from eval.metrics import (
    METRICS,
    citation_ok,
    delta_table,
    false_refusal,
    foreign_tenant_rate,
    hit_at_k,
    recall_at_k,
    refusal_ok,
    summarize,
)

RESULTS_DIR = Path("eval/results")
PIPELINE_ERROR_PREFIX = "ERROR:"
CSV_FIELDS = [
    "id",
    "tenant",
    "persona",
    "type",
    "classification",
    "question",
    "reference_answer",
    "source_doc_ids",
    "retrieved_doc_ids",
    "cited_doc_ids",
    "refused",
    *METRICS,
    "judge_rationale",
    "answer",
    "latency_ms",
    "answer_model",
    "judge_model",
]


@dataclass
class PipelineOutput:
    """What the scorer needs from one pipeline call."""

    answer: str
    refused: bool
    retrieved_doc_ids: list[str]
    cited_doc_ids: list[str]
    latency_ms: float
    model: str = ""  # model that actually answered (may be a fallback)


# Called with the whole eval row (question, tenant, persona).
Pipeline = Callable[[dict], PipelineOutput]


def load_eval_set(path: str | Path) -> list[dict]:
    """Read JSONL rows."""
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def score_row(q: dict, out: PipelineOutput, judge: Judge) -> dict:
    """Compute all per-question metrics for one pipeline output."""
    src = q.get("source_doc_ids", [])
    try:
        if out.answer.startswith(PIPELINE_ERROR_PREFIX):
            # No answer to grade; don't spend judge quota. Still counted as wrong (fail closed).
            raise RuntimeError("pipeline error, judge skipped")
        verdict = judge.grade(
            question=q["question"],
            reference=q["reference_answer"],
            answer=out.answer,
            qtype=q["type"],
        )
    except Exception as exc:  # noqa: BLE001 - fail closed: an ungradable answer is not correct
        verdict = Verdict(0, f"JUDGE_ERROR: {type(exc).__name__}: {str(exc)[:120]}")
    return {
        "id": q["id"],
        "tenant": q["tenant"],
        "persona": q["persona"],
        "type": q["type"],
        "classification": q["classification"],
        "question": q["question"],
        "reference_answer": q["reference_answer"],
        "source_doc_ids": "|".join(src),
        "retrieved_doc_ids": "|".join(out.retrieved_doc_ids),
        "cited_doc_ids": "|".join(out.cited_doc_ids),
        "refused": out.refused,
        "hit_at_k": hit_at_k(src, out.retrieved_doc_ids),
        "recall_at_k": recall_at_k(src, out.retrieved_doc_ids),
        "correct": verdict.correct,
        "citation_ok": citation_ok(src, out.retrieved_doc_ids, out.cited_doc_ids),
        "refusal_ok": refusal_ok(q["type"], out.refused),
        "false_refusal": false_refusal(q["type"], out.refused),
        "foreign_tenant_rate": foreign_tenant_rate(q["tenant"], out.retrieved_doc_ids),
        "judge_rationale": verdict.rationale,
        "answer": out.answer,
        "latency_ms": round(out.latency_ms, 1),
        "answer_model": out.model,
        "judge_model": verdict.model,
    }


def manifest_identity():  # type: ignore[no-untyped-def]
    """Seed plan + persona -> AuthUser resolver built from the manifest (offline modes only).

    The service and memory modes run without logging in, so the caller identity comes from the
    same deterministic seed plan that ``scripts.seed`` writes to the database (same ids and
    roles). The HTTP mode logs in for real instead.
    """
    from app.admin.seed import build_plan
    from app.auth.policy import AuthUser
    from app.config import get_settings
    from app.ingestion.corpus import load_manifest

    plan = build_plan(load_manifest(get_settings().corpus_path))
    users = {u.key: u for u in plan.users}

    def resolve(tenant: str, persona: str) -> AuthUser:
        u = users[persona_key(tenant, persona)]
        return AuthUser(
            id=u.id, tenant_id=u.tenant_id, roles=frozenset(u.roles), is_active=u.is_active
        )

    return plan, resolve


def memory_service():
    """RAGService over an in-memory EXACT cosine index of the corpus (no database).

    Same chunker, embedder, prompt, LLM and post-retrieval check as production; the vector search
    is exact numpy cosine, and the ACL pre-filter is ``can_access`` applied before ranking (the
    in-memory analogue of the SQL WHERE clause). ACLs come from the seed plan. Used for offline
    runs and as an exact-search reference for HNSW recall.
    """
    from contextlib import nullcontext

    import numpy as np

    from app.auth.policy import DocACL, can_access
    from app.config import get_settings
    from app.generation.llm import get_chain
    from app.ingestion.chunker import embedding_input
    from app.ingestion.corpus import load_documents
    from app.ingestion.embedder import get_embedder
    from app.ingestion.pipeline import build_chunks
    from app.retrieval.authorize import DocumentRecord
    from app.retrieval.retriever import RetrievedChunk
    from app.retrieval.service import RAGService

    s = get_settings()
    plan, _ = manifest_identity()
    docs = {
        d.id: DocumentRecord(
            d.id, d.tenant_id, d.classification, d.allowed_roles, d.allowed_users, 1
        )
        for d in plan.documents
    }
    emb = get_embedder(s.embedding_model, s.embedding_dim)
    chunks = build_chunks(
        load_documents(s.corpus_path), emb, size=s.chunk_size, overlap=s.chunk_overlap
    )
    mat = np.asarray(emb.embed_documents([embedding_input(c) for c in chunks]), dtype=np.float32)

    def row(i: int, dist: float) -> RetrievedChunk:
        c, d = chunks[i], docs[chunks[i].doc_id]
        return RetrievedChunk(
            c.chunk_id, c.doc_id, c.title, c.chunk_index, c.text, dist,
            tenant_id=d.tenant_id, classification=d.classification,
            allowed_roles=d.allowed_roles, allowed_users=d.allowed_users, acl_version=1,
        )  # fmt: skip

    def search(_session, qvec, k, allowed):  # ``allowed`` = predicate from acl_clause_fn
        dist = 1.0 - mat @ np.asarray(qvec, dtype=np.float32)
        out = []
        for i in np.argsort(dist, kind="stable"):
            r = row(int(i), float(dist[i]))
            if allowed(r):
                out.append(r)
                if len(out) == k:
                    break
        return out

    def acl_predicate(user):  # type: ignore[no-untyped-def]
        def allowed(r: RetrievedChunk) -> bool:
            acl = DocACL(r.doc_id, r.tenant_id, r.classification, frozenset(r.allowed_roles),
                         frozenset(r.allowed_users))  # fmt: skip
            return can_access(user, acl)

        return allowed

    return RAGService(
        embedder=emb,
        llm=get_chain(s.answer_specs(), s.llm_keys()),
        top_k=s.top_k,
        session_factory=nullcontext,
        search_fn=search,
        acl_clause_fn=acl_predicate,  # type: ignore[arg-type]
        rls_fn=lambda _s, _u: True,
        doc_loader=lambda _s, ids: {i: docs[i] for i in ids if i in docs},
    )


def service_pipeline(svc=None) -> Pipeline:
    """Call the RAG service layer in-process, as the row's persona (same path as POST /ask)."""
    if svc is None:
        from app.api import get_rag_service

        svc = get_rag_service()
    _, resolve = manifest_identity()

    def run(q: dict) -> PipelineOutput:
        t = time.perf_counter()
        r = svc.ask(q["question"], resolve(q["tenant"], q["persona"]))
        return PipelineOutput(
            answer=r.answer,
            refused=r.refused,
            retrieved_doc_ids=[c.doc_id for c in r.retrieved],
            cited_doc_ids=list(dict.fromkeys(c.doc_id for c in r.cited)),
            latency_ms=(time.perf_counter() - t) * 1000,
            model=r.llm_model,
        )

    return run


SEED_USERS = Path("data/seed_users.json")


def persona_key(tenant: str, persona: str) -> str:
    """Seeded user key for an eval persona: ``finance`` -> ``acme:finance_1``."""
    if persona.startswith("user:"):
        return persona.removeprefix("user:")
    return f"{tenant}:{persona}_1"


class PersonaLogins:
    """Logs in as each eval persona once (dev credentials from data/seed_users.json)."""

    def __init__(self, client, path: Path = SEED_USERS) -> None:  # type: ignore[no-untyped-def]
        if not path.exists():
            raise FileNotFoundError(f"{path} missing: run `python -m scripts.seed` first")
        data = json.loads(path.read_text(encoding="utf-8"))
        self._password = data["password"]
        self._emails = {u["key"]: u["email"] for u in data["users"]}
        self._client = client
        self._tokens: dict[str, tuple[str, float]] = {}

    def headers(self, tenant: str, persona: str) -> dict[str, str]:
        """Bearer header for the persona, re-logging in shortly before the token expires."""
        key = persona_key(tenant, persona)
        tok = self._tokens.get(key)
        if tok is None or tok[1] < time.time() + 30:
            r = self._client.post(
                "/auth/login", json={"email": self._emails[key], "password": self._password}
            )
            r.raise_for_status()
            body = r.json()
            tok = (body["access_token"], time.time() + body["expires_in"])
            self._tokens[key] = tok
        return {"Authorization": f"Bearer {tok[0]}"}


def http_pipeline(base_url: str) -> Pipeline:
    """Call a running API, logged in as each row's persona (tenant + persona -> seeded user)."""
    import httpx

    from app.generation.prompt import is_refusal

    client = httpx.Client(base_url=base_url, timeout=120)

    logins = PersonaLogins(client)

    def run(q: dict) -> PipelineOutput:
        t = time.perf_counter()
        r = client.post(
            "/ask",
            json={"question": q["question"]},
            headers=logins.headers(q["tenant"], q["persona"]),
        )
        r.raise_for_status()
        body = r.json()
        ms = (time.perf_counter() - t) * 1000
        return PipelineOutput(
            answer=body["answer"],
            refused=is_refusal(body["answer"]),
            retrieved_doc_ids=[x["doc_id"] for x in body["retrieved"]],
            cited_doc_ids=list(dict.fromkeys(c["doc_id"] for c in body["citations"])),
            latency_ms=ms,
            model=body.get("model", ""),
        )

    return run


def run_eval(questions: Sequence[dict], pipeline: Pipeline, judge: Judge) -> list[dict]:
    """Score every question; a pipeline error is recorded as a wrong, unrefused answer."""
    rows = []
    for i, q in enumerate(questions, 1):
        try:
            out = pipeline(q)
        except Exception as exc:  # noqa: BLE001 - one bad question must not kill the run
            # LLMError / ConnectionError messages are written to never contain secrets or DSNs.
            msg = f"{PIPELINE_ERROR_PREFIX} {type(exc).__name__}: {str(exc)[:400]}"
            out = PipelineOutput(msg, False, [], [], 0.0)
        rows.append(score_row(q, out, judge))
        print(f"\r[{i}/{len(questions)}] {q['id']}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    return rows


def write_results(rows: list[dict], summary: dict, label: str) -> Path:
    """Write CSV + summary JSON; return the CSV path."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / f"{stamp}_{label}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in CSV_FIELDS})
    path.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return path


def is_errored(row: dict) -> bool:
    """True if the pipeline or the judge failed for this row (worth re-running)."""
    return str(row.get("answer", "")).startswith("ERROR:") or str(
        row.get("judge_rationale", "")
    ).startswith("JUDGE_ERROR:")


def rescore_citations(row: dict) -> dict:
    """Recompute citation fields of a saved row from its stored answer (no LLM call).

    Label ``Cn`` is the n-th retrieved chunk, and ``retrieved_doc_ids`` is stored in rank order,
    so citations can be re-derived exactly. Used by ``--resume`` so that kept rows are scored by
    the current citation parser, not the one that was in place when they were produced.
    """
    from app.generation.prompt import cited_labels

    retrieved = [d for d in str(row.get("retrieved_doc_ids", "")).split("|") if d]
    src = [d for d in str(row.get("source_doc_ids", "")).split("|") if d]
    cited: list[str] = []
    for lab in cited_labels(str(row.get("answer", ""))):
        i = int(lab[1:]) - 1
        if 0 <= i < len(retrieved) and retrieved[i] not in cited:
            cited.append(retrieved[i])
    return {
        **row,
        "cited_doc_ids": "|".join(cited),
        "citation_ok": citation_ok(src, retrieved, cited),
    }


def fill_models(row: dict, csv_path: str | Path) -> dict:
    """Fill answer/judge model on rows from CSVs written before those columns existed.

    Such runs had no fallback, so the models are the ones recorded in the run's summary.json.
    """
    if row.get("answer_model") and row.get("judge_model"):
        return row
    meta_path = Path(csv_path).with_suffix(".summary.json")
    meta = (
        json.loads(meta_path.read_text(encoding="utf-8")).get("meta", {})
        if meta_path.exists()
        else {}
    )
    return {
        **row,
        "answer_model": row.get("answer_model") or meta.get("llm_model", ""),
        "judge_model": row.get("judge_model") or meta.get("judge_model", ""),
    }


def _count(values) -> dict[str, int]:  # type: ignore[no-untyped-def]
    """Value -> count, most common first, empty values dropped."""
    from collections import Counter

    return dict(Counter(v for v in values if v).most_common())


def read_rows(path: str | Path) -> list[dict]:
    """Load a results CSV."""
    with Path(path).open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _fmt(v: object) -> str:
    return "-" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))


def print_summary(summary: dict, title: str) -> None:
    """Overall + per-type table."""
    cols = ("n", *METRICS)
    print(f"\n== {title} ==")
    w = {c: max(9, len(c) + 2) for c in cols}
    print(f"{'group':<26}" + "".join(f"{c:>{w[c]}}" for c in cols))
    groups = [("overall", summary["overall"])]
    groups += [(f"type={k}", v) for k, v in summary.get("by_type", {}).items()]
    groups += [(f"tenant={k}", v) for k, v in summary.get("by_tenant", {}).items()]
    groups += [(f"class={k}", v) for k, v in summary.get("by_classification", {}).items()]
    by_model = summary.get("by_answer_model", {})
    if len(by_model) > 1:
        groups += [(f"model={k}", v) for k, v in by_model.items()]
    for name, g in groups:
        print(f"{name:<26}" + "".join(f"{_fmt(g.get(c)):>{w[c]}}" for c in cols))


def compare(a_path: str, b_path: str) -> None:
    """Print a delta table between two result CSVs."""
    a, b = summarize(read_rows(a_path)), summarize(read_rows(b_path))
    print(f"\nA = {a_path}\nB = {b_path}\n")
    print(f"{'metric':<22}{'A':>10}{'B':>10}{'B-A':>10}")
    for m, va, vb, d in delta_table(a, b):
        sign = "" if d is None or d <= 0 else "+"
        print(f"{m:<22}{_fmt(va):>10}{_fmt(vb):>10}{(sign + _fmt(d)):>10}")


def build_judge(kind: str) -> Judge:
    """``fake`` or ``llm`` (the configured LLM_PROVIDER/LLM_MODEL, e.g. Groq)."""
    if kind == "fake":
        return FakeJudge()
    from app.config import get_settings
    from app.generation.llm import build_chain

    s = get_settings()
    if s.llm_provider == "fake":
        return FakeJudge()
    return LLMJudge(build_chain(s.judge_specs(), dict(s.llm_keys())))


def _quiet_logs() -> None:
    import logging as _l

    for name in ("httpx", "httpx2", "huggingface_hub", "sentence_transformers"):
        _l.getLogger(name).setLevel(_l.WARNING)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    _quiet_logs()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--label", default="run")
    ap.add_argument("--eval-set", default="data/eval/eval_set.jsonl")
    ap.add_argument("--mode", choices=["service", "http", "memory"], default="service")
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--judge", choices=["llm", "fake"], default="llm")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--resume",
        metavar="CSV",
        help="continue a run cut short (e.g. free-tier daily limit): keep its rows except "
        "errored ones, run only the missing ids, and write a new combined CSV",
    )
    ap.add_argument(
        "--no-fallback",
        action="store_true",
        help="use only LLM_MODEL / JUDGE_MODEL (ignore LLM_FALLBACKS / JUDGE_FALLBACKS), so every "
        "row is answered by one model; rows that fail are left for --resume",
    )
    ap.add_argument("--compare", nargs=2, metavar=("A_CSV", "B_CSV"))
    args = ap.parse_args(argv)
    if args.no_fallback:
        import os

        from app.config import get_settings as _gs

        os.environ["LLM_FALLBACKS"] = ""
        os.environ["JUDGE_FALLBACKS"] = ""
        _gs.cache_clear()

    if args.compare:
        compare(*args.compare)
        return 0

    questions = load_eval_set(args.eval_set)
    if args.limit:
        questions = questions[: args.limit]
    if args.mode == "http":
        pipeline = http_pipeline(args.base_url)
    elif args.mode == "memory":
        pipeline = service_pipeline(memory_service())
    else:
        pipeline = service_pipeline()
    judge = build_judge(args.judge)
    kept: list[dict] = []
    if args.resume:
        kept = [
            rescore_citations(fill_models(r, args.resume))
            for r in read_rows(args.resume)
            if not is_errored(r)
        ]
        done = {r["id"] for r in kept}
        questions = [q for q in questions if q["id"] not in done]
        print(f"resume: keeping {len(kept)} rows, running {len(questions)}", file=sys.stderr)
    rows = kept + run_eval(questions, pipeline, judge)
    order = {q["id"]: i for i, q in enumerate(load_eval_set(args.eval_set))}
    rows.sort(key=lambda r: order.get(r["id"], 10**6))
    summary = summarize(rows, group_by=("type", "tenant", "classification", "answer_model"))
    from app.config import get_settings

    s = get_settings()
    summary["meta"] = {
        "label": args.label,
        "mode": args.mode,
        "judge": judge.name,
        "llm_provider": s.llm_provider,
        "llm_model": s.llm_model,
        "judge_model": (s.judge_model or s.llm_model) if judge.name == "llm" else "fake",
        "errored_rows": sum(1 for r in rows if is_errored(r)),
        "fallback": not args.no_fallback,
        "answer_models": _count(r.get("answer_model") for r in rows if not is_errored(r)),
        "judge_models": _count(r.get("judge_model") for r in rows if not is_errored(r)),
        "embedding_model": s.embedding_model,
        "top_k": s.top_k,
        "chunk_size": s.chunk_size,
        "chunk_overlap": s.chunk_overlap,
        "n_questions": len(rows),
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    path = write_results(rows, summary, args.label)
    print_summary(summary, f"{args.label} ({judge.name} judge)")
    print(f"\nwrote {path} and {path.with_suffix('.summary.json')}")
    models = summary["meta"]["answer_models"]
    if len(models) > 1:
        print(
            f"\nMIXED MODELS: answers came from {models}. Overall numbers mix models; read the "
            "answer_model rows above, or re-run with --no-fallback for a single-model baseline.",
            file=sys.stderr,
        )
    errored = summary["meta"]["errored_rows"]
    if errored:
        print(
            f"\nINCOMPLETE: {errored} row(s) errored and are scored as wrong. Do not report these "
            f"numbers; finish the run with:\n  python -m eval.run_eval --label {args.label} "
            f"--resume {path.as_posix()}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
