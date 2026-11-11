"""Pure metric functions (unit-tested with hand-made fixtures in tests/unit/test_metrics.py).

Definitions (k = TOP_K, doc-level, because answers are judged per document):
- hit@k        1 if any retrieved chunk comes from a source doc (answerable questions only).
- recall@k     |source_docs ∩ retrieved_docs| / |source_docs|  (answerable only).
- citation_ok  1 if the answer cites >=1 doc, every cited doc was retrieved, and at least one cited
               doc is a source doc (answerable only; a refusal scores 0).
- refusal_ok   1 if an unanswerable question was refused (unanswerable only).
- false_refusal 1 if an answerable question was refused (answerable only).
- correct      judge verdict (all questions; for unanswerable the judge checks the refusal).
- foreign_tenant_rate  share of retrieved chunks owned by a tenant other than the asker's
               (all questions). In the no-ACL baseline this is the cross-tenant exposure that
               reaches the LLM prompt; after Phase 2 it must be exactly 0.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from statistics import mean

METRICS = (
    "hit_at_k",
    "recall_at_k",
    "correct",
    "citation_ok",
    "refusal_ok",
    "false_refusal",
    "foreign_tenant_rate",
)


def hit_at_k(source_doc_ids: Sequence[str], retrieved_doc_ids: Sequence[str]) -> int | None:
    """1/0, or None when the question has no source docs (unanswerable)."""
    if not source_doc_ids:
        return None
    return int(bool(set(source_doc_ids) & set(retrieved_doc_ids)))


def recall_at_k(source_doc_ids: Sequence[str], retrieved_doc_ids: Sequence[str]) -> float | None:
    """Fraction of source docs present in the retrieved set, or None if unanswerable."""
    src = set(source_doc_ids)
    if not src:
        return None
    return len(src & set(retrieved_doc_ids)) / len(src)


def citation_ok(
    source_doc_ids: Sequence[str], retrieved_doc_ids: Sequence[str], cited_doc_ids: Sequence[str]
) -> int | None:
    """See module docstring. None for unanswerable questions."""
    if not source_doc_ids:
        return None
    cited = set(cited_doc_ids)
    if not cited:
        return 0
    return int(cited <= set(retrieved_doc_ids) and bool(cited & set(source_doc_ids)))


def refusal_ok(qtype: str, refused: bool) -> int | None:
    """1 if an unanswerable question was refused; None for answerable questions."""
    return int(refused) if qtype == "unanswerable" else None


def false_refusal(qtype: str, refused: bool) -> int | None:
    """1 if an answerable question was refused; None for unanswerable questions."""
    return None if qtype == "unanswerable" else int(refused)


def foreign_tenant_rate(tenant: str, retrieved_doc_ids: Sequence[str]) -> float | None:
    """Fraction of retrieved chunks whose doc_id is not prefixed ``<tenant>-``."""
    if not retrieved_doc_ids:
        return None
    foreign = sum(1 for d in retrieved_doc_ids if not d.startswith(f"{tenant}-"))
    return foreign / len(retrieved_doc_ids)


def _avg(values: Iterable[float | int | None]) -> float | None:
    xs = [float(v) for v in values if v is not None and v != ""]
    return round(mean(xs), 4) if xs else None


def summarize(
    rows: Sequence[dict], group_by: Sequence[str] = ("type", "tenant", "classification")
) -> dict:
    """Aggregate per-question rows into overall + per-group means (None-aware)."""

    def agg(rs: Sequence[dict]) -> dict:
        out: dict = {"n": len(rs)}
        for m in METRICS:
            out[m] = _avg(_to_num(r.get(m)) for r in rs)
        return out

    summary: dict = {"overall": agg(rows)}
    for g in group_by:
        keys = sorted({str(r.get(g)) for r in rows})
        summary[f"by_{g}"] = {k: agg([r for r in rows if str(r.get(g)) == k]) for k in keys}
    return summary


def _to_num(v: object) -> float | None:
    """CSV round-trip helper: '' / None -> None, 'True'/'False' -> 1/0, numbers -> float."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, int | float):
        return float(v)
    s = str(v).strip().lower()
    if s in ("true", "false"):
        return 1.0 if s == "true" else 0.0
    return float(s)


def delta_table(a: dict, b: dict) -> list[tuple[str, float | None, float | None, float | None]]:
    """(metric, a, b, b - a) for overall metrics of two summaries."""
    rows = []
    for m in ("n", *METRICS):
        va, vb = a["overall"].get(m), b["overall"].get(m)
        d = round(vb - va, 4) if va is not None and vb is not None else None
        rows.append((m, va, vb, d))
    return rows
