"""Eval-set tooling.

    python -m scripts.gen_eval draft --tenant acme --per-doc 2   # LLM drafts candidates -> data/eval/drafts/llm_<tenant>.jsonl
    python -m scripts.gen_eval build                             # merge drafts -> data/eval/eval_set.jsonl (+ evidence.jsonl)
    python -m scripts.gen_eval review                            # checks + data/eval/REVIEW.md

Drafts (one JSONL per source in data/eval/drafts/) carry an ``evidence`` list of verbatim quotes.
``build`` keeps only the schema fields in eval_set.jsonl and stores evidence in a sidecar so the
review can show *why* each reference answer is supported. ``review`` flags any row whose reference
answer is not literally supported by its source text, whose persona cannot read its sources, or
whose classification label disagrees with the manifest. Exit code 1 if anything is flagged.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from app.auth.policy import AuthUser, DocACL, can_access
from app.config import get_settings
from app.ingestion.corpus import CLASSIFICATIONS, load_manifest

EVAL_DIR = Path("data/eval")
DRAFTS = EVAL_DIR / "drafts"
EVAL_SET = EVAL_DIR / "eval_set.jsonl"
EVIDENCE = EVAL_DIR / "evidence.jsonl"
REVIEW = EVAL_DIR / "REVIEW.md"
SCHEMA = (
    "id",
    "tenant",
    "persona",
    "question",
    "reference_answer",
    "source_doc_ids",
    "classification",
    "type",
)
TYPES = ("factual", "multi_chunk", "unanswerable")

_NUM = re.compile(r"(?<![A-Za-z\d-])\$?\d[\d,]*(?:\.\d+)?%?|\b[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)+\b")


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _key_tokens(s: str) -> set[str]:
    return {t.replace(",", "").rstrip(".") for t in _NUM.findall(s)}


# ------------------------------------------------------------------ draft (LLM)
DRAFT_SYSTEM = """You write evaluation questions for a company-documents assistant.
Given ONE document, write questions an employee might ask whose answer is stated in the document.
Return JSON lines only, one object per line, with keys:
"question", "reference_answer", "evidence" (a list with 1-2 EXACT verbatim quotes copied from the
document that support the answer), "type" ("factual").
Do not name the company in the question. Every number in reference_answer must appear in evidence."""


def cmd_draft(args: argparse.Namespace) -> int:
    """Ask the configured LLM for candidate questions per document."""
    from app.generation.llm import build_llm

    s = get_settings()
    if s.llm_provider == "groq":
        s.require("groq_api_key")
    llm = build_llm(s.llm_provider, s.llm_model, s.groq_api_key)
    corpus = Path(s.corpus_path)
    manifest = load_manifest(corpus)
    docs = [d for d in manifest["documents"] if not args.tenant or d["tenant"] == args.tenant]
    out_rows, rejected = [], 0
    for d in docs:
        text = (corpus / d["path"]).read_text(encoding="utf-8")
        user = f"Write {args.per_doc} questions.\n\nDOCUMENT (doc_id={d['doc_id']}):\n{text}"
        raw = llm.complete(DRAFT_SYSTEM, user, max_tokens=4000).text
        for line in raw.splitlines():
            line = line.strip().strip(",")
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                rejected += 1
                continue
            ev = [e for e in obj.get("evidence", []) if _norm_ws(e) in _norm_ws(text)]
            if not ev:
                rejected += 1
                continue
            persona = (
                d["allowed_roles"][0] if d["allowed_roles"] else f"user:{d['allowed_users'][0]}"
            )
            out_rows.append(
                {
                    "id": "",
                    "tenant": d["tenant"],
                    "persona": persona,
                    "question": obj["question"],
                    "reference_answer": obj["reference_answer"],
                    "source_doc_ids": [d["doc_id"]],
                    "classification": d["classification"],
                    "type": "factual",
                    "evidence": ev,
                }
            )
    DRAFTS.mkdir(parents=True, exist_ok=True)
    path = DRAFTS / f"llm_{args.tenant or 'all'}.jsonl"
    for i, r in enumerate(out_rows, 1):
        r["id"] = f"{r['tenant']}-llm{i:03d}"
    path.write_text("".join(json.dumps(r) + "\n" for r in out_rows), encoding="utf-8")
    print(
        f"wrote {len(out_rows)} candidates to {path} ({rejected} rejected: bad JSON or non-verbatim evidence)"
    )
    print(
        "Candidates are NOT in the eval set until you move the ones you accept into a reviewed draft file and run `build`."
    )
    return 0


# ------------------------------------------------------------------ build
def cmd_build(args: argparse.Namespace) -> int:
    """Merge reviewed draft files into eval_set.jsonl + evidence.jsonl."""
    files = sorted(p for p in DRAFTS.glob("*.jsonl") if not p.name.startswith("llm_"))
    rows: list[dict] = []
    for p in files:
        rows += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    ids = [r["id"] for r in rows]
    dup = [k for k, v in Counter(ids).items() if v > 1]
    if dup:
        print(f"duplicate ids: {dup}", file=sys.stderr)
        return 1
    EVAL_SET.write_text(
        "".join(json.dumps({k: r[k] for k in SCHEMA}) + "\n" for r in rows), encoding="utf-8"
    )
    EVIDENCE.write_text(
        "".join(
            json.dumps({"id": r["id"], "evidence": r.get("evidence", [])}) + "\n" for r in rows
        ),
        encoding="utf-8",
    )
    print(f"wrote {len(rows)} rows from {len(files)} draft files to {EVAL_SET}")
    return 0


# ------------------------------------------------------------------ review
def persona_user(tenant: str, persona: str) -> AuthUser:
    """Principal for an eval persona: a role name (``finance``) or ``user:<tenant>:<slot>``.

    A role persona is that tenant's ``<role>_1`` user holding just that role; a user persona is
    that exact user id. Used only for offline checks against manifest ACLs.
    """
    from app.admin.seed import tenant_uuid, user_uuid

    if persona.startswith("user:"):
        key = persona.removeprefix("user:")
        return AuthUser(
            id=user_uuid(key), tenant_id=tenant_uuid(tenant), roles=frozenset({"employee"})
        )
    return AuthUser(
        id=user_uuid(f"{tenant}:{persona}_1"),
        tenant_id=tenant_uuid(tenant),
        roles=frozenset({persona}),
    )


def manifest_acl(d: dict) -> DocACL:
    """DocACL from a manifest entry (user keys resolved to the seeded user ids)."""
    from app.admin.seed import tenant_uuid, user_uuid

    return DocACL(
        doc_id=d["doc_id"],
        tenant_id=tenant_uuid(d["tenant"]),
        classification=d["classification"],
        allowed_roles=frozenset(d.get("allowed_roles", [])),
        allowed_users=frozenset(user_uuid(k) for k in d.get("allowed_users", [])),
    )


def check_row(
    row: dict, evidence: list[str], docs: dict[str, dict], texts: dict[str, str]
) -> list[str]:
    """Return a list of flags for one eval row (empty == looks supported)."""
    flags = []
    missing_keys = [k for k in SCHEMA if k not in row]
    if missing_keys:
        return [f"missing fields {missing_keys}"]
    if row["type"] not in TYPES:
        flags.append(f"bad type {row['type']}")
    if row["classification"] not in CLASSIFICATIONS:
        flags.append(f"bad classification {row['classification']}")
    srcs = row["source_doc_ids"]
    if row["type"] == "unanswerable":
        if srcs:
            flags.append("unanswerable question lists source docs")
        return flags
    if not srcs:
        return flags + ["answerable question has no source docs"]
    if row["type"] == "multi_chunk" and len(srcs) == 1 and len(evidence) < 2:
        flags.append("multi_chunk question with a single source and <2 evidence quotes")
    unknown = [s for s in srcs if s not in docs]
    if unknown:
        return flags + [f"unknown source doc(s) {unknown}"]
    for s in srcs:
        if docs[s]["tenant"] != row["tenant"]:
            flags.append(f"source {s} belongs to another tenant")
    source_text = _norm_ws(" ".join(texts[s] for s in srcs))
    if not evidence:
        flags.append("no evidence quotes")
    for e in evidence:
        if _norm_ws(e) not in source_text:
            flags.append(f"evidence not verbatim in sources: {e[:70]!r}")
    unsupported = sorted(
        _key_tokens(row["reference_answer"]) - _key_tokens(" ".join(texts[s] for s in srcs))
    )
    if unsupported:
        flags.append(f"reference facts not found in source text: {unsupported}")
    # persona must be allowed to read every source, judged by the real policy function
    persona = row["persona"]
    user = persona_user(row["tenant"], persona)
    for s in srcs:
        if not can_access(user, manifest_acl(docs[s])):
            flags.append(f"persona {persona} cannot read {s}")
    rank = {c: i for i, c in enumerate(CLASSIFICATIONS)}
    expected = max((docs[s]["classification"] for s in srcs), key=rank.__getitem__)
    if row["classification"] != expected:
        flags.append(f"classification {row['classification']} but highest source is {expected}")
    return flags


def cmd_review(args: argparse.Namespace) -> int:
    """Write REVIEW.md listing every question with reference, sources, evidence and flags."""
    s = get_settings()
    corpus = Path(s.corpus_path)
    manifest = load_manifest(corpus)
    docs = {d["doc_id"]: d for d in manifest["documents"]}
    texts = {k: (corpus / d["path"]).read_text(encoding="utf-8") for k, d in docs.items()}
    rows = [json.loads(x) for x in EVAL_SET.read_text(encoding="utf-8").splitlines() if x.strip()]
    ev = {}
    if EVIDENCE.exists():
        for x in EVIDENCE.read_text(encoding="utf-8").splitlines():
            if x.strip():
                o = json.loads(x)
                ev[o["id"]] = o["evidence"]
    results = [
        (r, ev.get(r["id"], []), check_row(r, ev.get(r["id"], []), docs, texts)) for r in rows
    ]
    flagged = [(r, f) for r, _, f in results if f]

    c_type = Counter(r["type"] for r in rows)
    c_cls = Counter(r["classification"] for r in rows)
    c_ten = Counter(r["tenant"] for r in rows)
    lines = [
        "# Eval set review",
        "",
        "Generated by `python -m scripts.gen_eval review`. Check every row by hand: does the reference",
        "answer follow from the evidence, and is the question unambiguous without naming the company?",
        "",
        f"- Questions: **{len(rows)}**",
        f"- By type: {dict(c_type)}",
        f"- By classification: {dict(c_cls)}",
        f"- By tenant: {dict(c_ten)}",
        f"- **Flagged: {len(flagged)}**",
        "",
    ]
    if flagged:
        lines += ["## Flagged rows", ""]
        for r, f in flagged:
            lines.append(f"- `{r['id']}`: " + "; ".join(f))
        lines.append("")
    lines += ["## All questions", ""]
    for r, e, f in results:
        status = (
            "FLAGGED: " + "; ".join(f)
            if f
            else "ok (evidence verbatim, facts found in source, persona can read sources)"
        )
        lines += [
            f"### {r['id']} - {r['tenant']} / {r['type']} / {r['classification']} / persona `{r['persona']}`",
            "",
            f"**Q:** {r['question']}",
            "",
            f"**Reference:** {r['reference_answer']}",
            "",
            f"**Sources:** {', '.join(f'`{x}`' for x in r['source_doc_ids']) or '(none)'}",
            "",
        ]
        for q in e:
            lines.append(f"> {_norm_ws(q)}")
            lines.append(">")
        if e:
            lines.pop()
            lines.append("")
        lines += [f"**Check:** {status}", "", "- [ ] reviewed by hand", ""]
    REVIEW.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {REVIEW}: {len(rows)} questions, {len(flagged)} flagged")
    for r, f in flagged:
        print(f"  {r['id']}: {'; '.join(f)}")
    return 1 if flagged else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft")
    d.add_argument("--tenant", default=None)
    d.add_argument("--per-doc", type=int, default=2)
    sub.add_parser("build")
    sub.add_parser("review")
    args = ap.parse_args(argv)
    return {"draft": cmd_draft, "build": cmd_build, "review": cmd_review}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
