"""The adversarial leakage suite: every attack x persona against the live API. Must print LEAKS: 0.

Writes tests/security/report.json and tests/security/REPORT.md. Set SECURITY_PERSONAS_PER_QUESTION
to run each question as more personas (default 4; 21 = the full cross product).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tests.security.attacks import ask_attacks, http_attacks, positive_controls
from tests.security.dynamic import (
    ALL_PERSONA_FAMILIES,
    account_attacks,
    account_controls,
    check_integrity,
    dynamic_ask_attacks,
    dynamic_controls,
    upload_attacks,
)
from tests.security.runner import check_audit, run_suite, summary

pytestmark = [pytest.mark.neon, pytest.mark.security]
OUT = Path(__file__).parent


def _markdown(rep: dict) -> str:
    lines = [
        "# Leakage suite report",
        "",
        f"Attack queries: **{rep['attack_queries_total']}** ({rep['ask_questions']} questions, "
        f"{rep['http_attacks']} crafted HTTP attacks) | requests: {rep['requests_total']} | "
        f"personas: {len(rep['personas'])} ({rep['personas_attacking']} sent requests) | "
        f"positive controls ok: {rep['positive_controls_ok']} | duration {rep['duration_s']} s",
        "",
        "| family | queries | requests | leaks | failures |",
        "|---|---:|---:|---:|---:|",
    ]
    for fam, v in rep["families"].items():
        lines.append(
            f"| {fam} | {v['queries']} | {v['requests']} | {v['leaks']} | {v['failures']} |"
        )
    lines += ["", f"**LEAKS: {rep['leaks']}** | other failures: {rep['failures']}", ""]
    return "\n".join(lines)


def test_no_leaks_for_any_persona_or_attack(api_server, dynamic_world):
    """Seeded tenants + companies registered at test time (Phase 6), one oracle for all."""
    w, o = dynamic_world
    asks = ask_attacks(o) + dynamic_ask_attacks(w)
    https = http_attacks(o) + upload_attacks(o, w) + account_attacks(o, w)  # + Phase 7
    controls = positive_controls(o) + dynamic_controls(w) + account_controls(w)
    upload_fams = {h.family for h in https if h.family.startswith(("upload", "registration"))}
    assert len(asks) + len(https) >= 400, len(asks) + len(https)
    assert len(upload_fams) >= 6, upload_fams
    assert len({a.family for a in asks} | {h.family for h in https}) >= 20
    t0 = time.perf_counter()
    res = run_suite(
        api_server, o, asks, https, controls,
        personas_per_question=int(os.environ.get("SECURITY_PERSONAS_PER_QUESTION", "4")),
        all_persona_families=ALL_PERSONA_FAMILIES,
    )  # fmt: skip
    time.sleep(5)  # let the last background ingestions (bomb uploads) finish before the audit
    res.audit_problems = check_audit(o, res) + check_integrity(w)
    rep = summary(o, asks, https, res, time.perf_counter() - t0)
    (OUT / "report.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    md = _markdown(rep)
    (OUT / "REPORT.md").write_text(md, encoding="utf-8")
    print("\n" + md)
    assert rep["positive_controls_ok"], rep["positive_controls"]
    assert rep["leaks"] == 0, json.dumps(rep["leak_details"][:10], indent=2, default=str)
    assert rep["failures"] == 0, json.dumps(rep["failure_details"][:10], indent=2, default=str)


@pytest.mark.skip(
    reason="Placeholder: a semantic answer cache (Project 1) must key on (tenant_id, acl "
    "fingerprint of the caller, normalized query) or it becomes a cross-tenant leak path. "
    "Enable this test when a cache exists: same question by two tenants must miss each other."
)
def test_semantic_cache_key_is_tenant_and_acl_aware():
    raise AssertionError("not implemented: no semantic cache yet")
