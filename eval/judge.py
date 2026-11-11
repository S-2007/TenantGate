"""Answer-correctness judges.

- ``FakeJudge``: deterministic rules, no network. Correct iff the answer contains every "key fact"
  token of the reference (numbers, money, percentages, codenames like PROJECT-X-Y) or, when the
  reference has none, >= 60% of its content words. Unanswerable: correct iff the answer refuses.
- ``LLMJudge``: the configured LLM (Groq, temperature 0, fixed seed) grades 0/1 with a
  one-sentence rationale and strict JSON output.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Protocol

from app.generation.llm import LLMClient
from app.generation.prompt import is_refusal


@dataclass(frozen=True)
class Verdict:
    """Binary correctness plus a short explanation."""

    correct: int
    rationale: str
    model: str = ""  # which judge model produced the verdict ("" for the fake judge)


class Judge(Protocol):
    """Anything that grades an answer against a reference."""

    name: str

    def grade(self, *, question: str, reference: str, answer: str, qtype: str) -> Verdict: ...


_KEY = re.compile(
    # numbers/money/% (not the digit in "Q2"/"L4"/"SEV1") or hyphenated CODE-NAMES
    r"(?<![A-Za-z\d-])\$?\d[\d,]*(?:\.\d+)?%?|\b[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)+\b"
)
_WORDS = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by for from has have in is it of on or the to was were with per each "
    "you your our their they this that these those can will may must not no".split()
)


def _norm_num(tok: str) -> str:
    return tok.replace(",", "").replace("$", "").rstrip(".").lower()


def key_facts(text: str) -> set[str]:
    """Numbers, amounts, percentages and hyphenated upper-case codes, normalised."""
    return {_norm_num(t) for t in _KEY.findall(text)}


class FakeJudge:
    """Deterministic judge used in tests and when no LLM is configured."""

    name = "fake"

    def grade(self, *, question: str, reference: str, answer: str, qtype: str) -> Verdict:
        """Rule-based verdict (see module docstring)."""
        if qtype == "unanswerable":
            ok = is_refusal(answer)
            return Verdict(
                int(ok), "refused as expected" if ok else "answered an unanswerable question"
            )
        if is_refusal(answer):
            return Verdict(0, "refused an answerable question")
        ref_keys = key_facts(reference)
        if ref_keys:
            ans_keys = key_facts(answer)
            missing = sorted(ref_keys - ans_keys)
            return Verdict(
                int(not missing),
                f"missing key facts: {missing}" if missing else "all key facts present",
            )
        ref_words = {w for w in _WORDS.findall(reference.lower()) if w not in _STOP}
        ans_words = set(_WORDS.findall(answer.lower()))
        recall = len(ref_words & ans_words) / max(1, len(ref_words))
        return Verdict(int(recall >= 0.6), f"content-word recall {recall:.2f}")


JUDGE_SYSTEM = """You grade answers from a company-documents assistant against a reference answer.
Return ONLY a JSON object: {"correct": 0 or 1, "rationale": "<one short sentence>"}.

Grading rules:
- correct=1 if the answer states the same key facts as the reference (numbers, names, dates, amounts
  must match exactly; extra correct detail is fine; wording may differ).
- correct=0 if any key fact is wrong or missing, or the answer refuses when the reference has an answer.
- If the reference says the documents do not contain the information, correct=1 only if the answer
  declines / says it does not know, and correct=0 if it gives a substantive answer."""


class LLMJudge:
    """LLM-backed judge (Groq by default) with strict JSON parsing; unparseable output counts as incorrect."""

    name = "llm"

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def grade(self, *, question: str, reference: str, answer: str, qtype: str) -> Verdict:
        """Ask the LLM for a 0/1 verdict."""
        user = (
            f"Question: {question}\n\nReference answer: {reference}\n\n"
            f"Answer to grade: {answer}\n\nReturn the JSON object now."
        )
        resp = self.llm.complete(JUDGE_SYSTEM, user, max_tokens=1024)
        return replace(parse_verdict(resp.text), model=resp.model)


def parse_verdict(raw: str) -> Verdict:
    """Extract the first JSON object from ``raw``; fail closed (0) if malformed."""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return Verdict(0, f"judge output unparseable: {raw[:80]!r}")
    try:
        obj = json.loads(m.group(0))
        correct = int(obj.get("correct", 0))
        if correct not in (0, 1):
            raise ValueError
        return Verdict(correct, str(obj.get("rationale", ""))[:300])
    except (ValueError, TypeError, json.JSONDecodeError):
        return Verdict(0, f"judge output invalid: {raw[:80]!r}")
