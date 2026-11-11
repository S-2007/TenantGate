"""Metric functions and the fake judge, on hand-made fixtures."""

import pytest

from eval.judge import FakeJudge, key_facts, parse_verdict
from eval.metrics import (
    citation_ok,
    delta_table,
    false_refusal,
    foreign_tenant_rate,
    hit_at_k,
    recall_at_k,
    refusal_ok,
    summarize,
)
from eval.run_eval import PipelineOutput, score_row


def test_hit_at_k():
    assert hit_at_k(["a"], ["x", "a", "y"]) == 1
    assert hit_at_k(["a"], ["x", "y"]) == 0
    assert hit_at_k([], ["x"]) is None


def test_recall_at_k_multi_source():
    assert recall_at_k(["a", "b"], ["a", "a", "c"]) == 0.5
    assert recall_at_k(["a", "b"], ["b", "a"]) == 1.0
    assert recall_at_k([], ["a"]) is None


@pytest.mark.parametrize(
    "cited,expected",
    [
        (["a"], 1),  # cites a retrieved source
        (["a", "c"], 1),  # extra retrieved non-source citation still ok
        (["c"], 0),  # cites only a non-source
        (["z"], 0),  # cites something never retrieved (hallucinated citation)
        (["a", "z"], 0),  # any unretrieved citation fails
        ([], 0),  # no citation
    ],
)
def test_citation_ok(cited, expected):
    assert citation_ok(["a", "b"], ["a", "c"], cited) == expected


def test_citation_ok_unanswerable_is_none():
    assert citation_ok([], ["a"], ["a"]) is None


def test_refusal_metrics():
    assert refusal_ok("unanswerable", True) == 1
    assert refusal_ok("unanswerable", False) == 0
    assert refusal_ok("factual", True) is None
    assert false_refusal("factual", True) == 1
    assert false_refusal("unanswerable", True) is None


def test_summarize_ignores_not_applicable_and_handles_csv_strings():
    rows = [
        {
            "type": "factual",
            "tenant": "acme",
            "classification": "public",
            "hit_at_k": 1,
            "recall_at_k": 1.0,
            "correct": 1,
            "citation_ok": 1,
            "refusal_ok": None,
            "false_refusal": 0,
        },
        {
            "type": "factual",
            "tenant": "acme",
            "classification": "internal",
            "hit_at_k": "0",
            "recall_at_k": "0.0",
            "correct": "0",
            "citation_ok": "0",
            "refusal_ok": "",
            "false_refusal": "1",
        },
        {
            "type": "unanswerable",
            "tenant": "lumen",
            "classification": "internal",
            "hit_at_k": "",
            "recall_at_k": "",
            "correct": 1,
            "citation_ok": "",
            "refusal_ok": 1,
            "false_refusal": "",
        },
    ]
    s = summarize(rows)
    o = s["overall"]
    assert o["n"] == 3 and o["hit_at_k"] == 0.5 and o["refusal_ok"] == 1.0
    assert o["correct"] == pytest.approx(0.6667, abs=1e-4)
    assert s["by_type"]["unanswerable"]["hit_at_k"] is None
    assert s["by_tenant"]["acme"]["n"] == 2


def test_delta_table():
    a = {
        "overall": {
            "n": 10,
            "hit_at_k": 0.8,
            "recall_at_k": 0.7,
            "correct": 0.6,
            "citation_ok": 0.5,
            "refusal_ok": 1.0,
            "false_refusal": 0.1,
        }
    }
    b = {"overall": {**a["overall"], "hit_at_k": 0.9, "refusal_ok": None}}
    d = {m: delta for m, _, _, delta in delta_table(a, b)}
    assert d["hit_at_k"] == pytest.approx(0.1) and d["refusal_ok"] is None and d["n"] == 0


def test_key_facts_ignore_codes_like_q2_but_keep_amounts_and_codenames():
    ks = key_facts(
        "Q2 revenue was $41.2 million; codename PROJECT-BLUEHERON-ACME, L4 band $142,500."
    )
    assert {"41.2", "142500", "project-blueheron-acme"} <= ks
    assert "2" not in ks and "4" not in ks


def test_fake_judge():
    j = FakeJudge()
    g = lambda ans, ref="You get 22 days of PTO.", t="factual": j.grade(  # noqa: E731
        question="q", reference=ref, answer=ans, qtype=t
    ).correct
    assert g("Employees receive 22 days [C1].") == 1
    assert g("Employees receive 25 days [C1].") == 0
    assert g("I don't know based on the available documents.") == 0
    assert g("I don't know based on the available documents.", "n/a", "unanswerable") == 1
    assert g("It is $40.", "n/a", "unanswerable") == 0


def test_parse_verdict_fails_closed():
    assert parse_verdict('{"correct": 1, "rationale": "ok"}').correct == 1
    assert parse_verdict('Sure! {"correct": 0, "rationale": "wrong number"}').correct == 0
    assert parse_verdict("looks right to me").correct == 0
    assert parse_verdict('{"correct": 7}').correct == 0


def test_score_row_end_to_end():
    q = {
        "id": "acme-q01",
        "tenant": "acme",
        "persona": "employee",
        "type": "factual",
        "classification": "internal",
        "question": "How many sick days?",
        "reference_answer": "10 sick days per year.",
        "source_doc_ids": ["acme-leave-policy"],
    }
    out = PipelineOutput(
        "You get 10 sick days [C1].",
        False,
        ["acme-leave-policy", "acme-handbook"],
        ["acme-leave-policy"],
        12.0,
    )
    r = score_row(q, out, FakeJudge())
    assert (r["hit_at_k"], r["recall_at_k"], r["correct"], r["citation_ok"]) == (1, 1.0, 1, 1)
    assert r["refusal_ok"] is None and r["false_refusal"] == 0


def test_foreign_tenant_rate():
    assert foreign_tenant_rate("acme", ["acme-a", "lumen-b", "northwind-c", "acme-d"]) == 0.5
    assert foreign_tenant_rate("acme", ["acme-a"]) == 0.0
    assert foreign_tenant_rate("acme", []) is None
    # prefix must include the dash: "acmex-..." is another tenant
    assert foreign_tenant_rate("acme", ["acmex-a"]) == 1.0


def test_judge_exception_fails_closed_and_is_marked_for_resume():
    from eval.run_eval import is_errored

    class BrokenJudge:
        name = "llm"

        def grade(self, **kw):
            raise RuntimeError("rate limited")

    q = {
        "id": "x",
        "tenant": "acme",
        "persona": "employee",
        "type": "factual",
        "classification": "internal",
        "question": "q",
        "reference_answer": "10 days",
        "source_doc_ids": ["acme-a"],
    }
    r = score_row(
        q, PipelineOutput("10 days [C1]", False, ["acme-a"], ["acme-a"], 1.0), BrokenJudge()
    )
    assert r["correct"] == 0 and r["judge_rationale"].startswith("JUDGE_ERROR")
    assert is_errored(r)
    assert is_errored({"answer": "ERROR: LLMError", "judge_rationale": ""})
    assert not is_errored({"answer": "10 days", "judge_rationale": "ok"})


# --- citation parsing must tolerate formats real models emit (seen in the Groq baseline) ---
@pytest.mark.parametrize(
    "answer,expected",
    [
        ("22 days. [C1]", ["C1"]),
        ("16 weeks [ C4 ].", ["C4"]),
        ("$75 per day.【C1】", ["C1"]),
        ("Over $20 [ C1 ][ C5 ].", ["C1", "C5"]),
        ("Both apply [C1, C3]; see also [C3].", ["C1", "C3"]),
        ("No citation here, and C2 alone is not one.", []),
    ],
)
def test_cited_labels_tolerant_formats(answer, expected):
    from app.generation.prompt import cited_labels

    assert cited_labels(answer) == expected


def test_rescore_citations_rebuilds_from_answer_and_rank_order():
    from eval.run_eval import rescore_citations

    row = {
        "answer": "16 weeks [ C2 ] and 14 weeks 【C1】.",
        "retrieved_doc_ids": "lumen-leave|acme-leave|acme-expense",
        "source_doc_ids": "acme-leave",
        "cited_doc_ids": "",  # what the old strict parser produced
        "citation_ok": "0",
    }
    out = rescore_citations(row)
    assert out["cited_doc_ids"] == "acme-leave|lumen-leave"
    assert out["citation_ok"] == 1


def test_pipeline_error_skips_judge_and_fails_closed():
    calls = []

    class SpyJudge:
        name = "spy"

        def grade(self, **kw):
            calls.append(kw)
            raise AssertionError("judge must not be called on a pipeline error")

    q = {
        "id": "x",
        "tenant": "acme",
        "persona": "acme:employee_1",
        "type": "factual",
        "classification": "internal",
        "question": "q",
        "reference_answer": "r",
        "source_doc_ids": ["acme-leave"],
    }
    r = score_row(
        q, PipelineOutput("ERROR: LLMError: rate limit (TPD)", False, [], [], 0.0), SpyJudge()
    )
    assert calls == [] and r["correct"] == 0
