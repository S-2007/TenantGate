"""Corpus, manifest, canary registry and eval set stay internally consistent (offline)."""

import json
from collections import Counter
from pathlib import Path

import pytest

from app.ingestion.corpus import load_manifest, validate_manifest
from scripts.gen_eval import EVAL_SET, EVIDENCE, check_row
from scripts.seed import check_canaries

CORPUS = Path("data/corpus")


@pytest.fixture(scope="module")
def manifest():
    return load_manifest(CORPUS)


def test_manifest_valid(manifest):
    assert validate_manifest(manifest, CORPUS) == []


def test_corpus_shape(manifest):
    docs = manifest["documents"]
    by_tenant = Counter(d["tenant"] for d in docs)
    assert set(by_tenant) == {"acme", "northwind", "lumen"}
    assert all(20 <= n <= 30 for n in by_tenant.values())
    for t in by_tenant:
        classes = {d["classification"] for d in docs if d["tenant"] == t}
        assert classes == {"public", "internal", "confidential"}
    assert any(d["allowed_users"] for d in docs), "need user-specific docs"
    salary = [d for d in docs if d["doc_id"].endswith("-salary-bands")]
    assert len(salary) == 3 and all(sorted(d["allowed_roles"]) == ["admin", "hr"] for d in salary)


def test_canaries_unique_to_their_tenant(manifest):
    assert check_canaries(CORPUS, manifest) == []


def test_eval_set_rows_supported(manifest):
    docs = {d["doc_id"]: d for d in manifest["documents"]}
    texts = {k: (CORPUS / d["path"]).read_text(encoding="utf-8") for k, d in docs.items()}
    rows = [json.loads(x) for x in EVAL_SET.read_text(encoding="utf-8").splitlines() if x.strip()]
    ev = {
        o["id"]: o["evidence"]
        for o in map(json.loads, EVIDENCE.read_text(encoding="utf-8").splitlines())
    }
    assert 80 <= len(rows) <= 100
    types = Counter(r["type"] for r in rows)
    assert 8 <= types["unanswerable"] <= 15
    assert {r["tenant"] for r in rows} == {"acme", "northwind", "lumen"}
    assert {r["classification"] for r in rows} == {"public", "internal", "confidential"}
    flagged = {r["id"]: check_row(r, ev.get(r["id"], []), docs, texts) for r in rows}
    assert {k: v for k, v in flagged.items() if v} == {}
