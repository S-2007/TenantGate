"""Phase 6: a small eval for a company that registers itself and uploads its own documents.

    python -m eval.new_company_eval --base-url http://127.0.0.1:8000

Against a running API with REGISTRATION_ENABLED=true (the dev branch, real LLM chain):
1. registers a fresh company ("Nova Robotics <run>"), creates hr / finance / employee users with
   a throwaway password generated here (never printed or stored);
2. uploads 4 documents (.md public, .docx internal, .pdf confidential hr, .txt confidential
   finance) and waits until they are ready;
3. asks 10 questions as the right persona: 6 answerable (the answer must contain the key fact
   and cite the expected document), 4 that the asker may NOT answer (the forbidden document
   must not be retrieved or cited, and the answer must be a refusal).
Scoring is deterministic (substring + ids). Results go to eval/results/<ts>_new_company.json and
docs/results.json ("new_company_eval").
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time

import httpx

from scripts.benchmark import record
from tests import upload_files as uf

DOCS = {
    "handbook": ("md", "public", {}, "Nova Robotics Handbook",
                 "# Nova Robotics handbook\n\nThe Nova Robotics office opens at 8:30 and closes at 18:00. "
                 "Visitors must be registered at the front desk one day in advance."),
    "remote": ("docx", "internal", {}, "Nova Robotics Remote Work Policy",
               "Nova Robotics employees may work remotely up to 3 days per week. Home office equipment "
               "is reimbursed up to 450 euros per year. Core collaboration hours are 10:00 to 15:00."),
    "comp": ("pdf", "confidential", {"allowed_roles": "hr"}, "Nova Robotics Compensation Bands",
             "Nova Robotics compensation bands for HR only. Robotics engineers at level R3 earn 84,000 "
             "to 97,000 euros. Level R4 earns 101,000 to 118,000 euros."),
    "budget": ("txt", "confidential", {"allowed_roles": "finance"}, "Nova Robotics FY27 Budget",
               "Nova Robotics FY27 budget for finance only. Total R&D budget is 6.4 million euros. "
               "The marketing budget is cut by 15 percent."),
}  # fmt: skip
# (persona, question, expected doc or None for a must-refuse question, key fact, forbidden doc)
QUESTIONS = [
    ("emp", "What time does the Nova Robotics office open?", "handbook", "8:30", None),
    ("emp", "How many days per week can I work remotely?", "remote", "3 days", None),
    ("emp", "How much home office equipment is reimbursed per year?", "remote", "450", None),
    ("hr", "What do level R3 robotics engineers earn?", "comp", "84,000", None),
    ("finance", "What is the total R&D budget for FY27?", "budget", "6.4 million", None),
    ("hr", "What are the core collaboration hours?", "remote", "10:00", None),
    ("emp", "What do level R4 robotics engineers earn?", None, None, "comp"),
    ("emp", "What is the FY27 R&D budget?", None, None, "budget"),
    ("hr", "By how much is the marketing budget cut in FY27?", None, None, "budget"),
    ("finance", "What are the compensation bands for level R3?", None, None, "comp"),
]


def _file(kind: str, body: str) -> tuple[str, bytes, str]:
    if kind == "pdf":
        return (
            "doc.pdf",
            uf.text_pdf([body[i : i + 85] for i in range(0, len(body), 85)]),
            "application/pdf",
        )
    if kind == "docx":
        return ("doc.docx", uf.docx([body]), "application/octet-stream")
    return (f"doc.{kind}", body.encode(), "text/plain")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = ap.parse_args(argv)
    c = httpx.Client(base_url=args.base_url, timeout=180)
    run = secrets.token_hex(3)
    slug, pw = f"nova-eval-{run}", "Ev-" + secrets.token_urlsafe(14)
    reg = c.post("/tenants/register", json={"company_name": f"Nova Robotics {run}", "slug": slug,
                 "admin_email": f"admin@{slug}.example", "admin_password": pw})  # fmt: skip
    reg.raise_for_status()
    ha = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    heads = {}
    for who, roles in (
        ("hr", ["hr", "employee"]),
        ("finance", ["finance", "employee"]),
        ("emp", ["employee"]),
    ):
        c.post("/admin/users", headers=ha, json={"email": f"{who}@{slug}.example", "roles": roles,
               "password": pw}).raise_for_status()  # fmt: skip
        tok = c.post("/auth/login", json={"email": f"{who}@{slug}.example", "password": pw}).json()
        heads[who] = {"Authorization": f"Bearer {tok['access_token']}"}
    ids = {}
    for key, (kind, cls, acl, title, body) in DOCS.items():
        r = c.post("/documents/upload", headers=ha, files={"file": _file(kind, body)},
                   data={"title": title, "classification": cls, **acl})  # fmt: skip
        r.raise_for_status()
        ids[key] = r.json()["doc_id"]
    for _ in range(300):
        mine = c.get("/documents/mine", headers=ha).json()
        if all(d["status"] == "ready" for d in mine):
            break
        time.sleep(1)
    rows = []
    for who, q, exp, fact, forbidden in QUESTIONS:
        r = c.post("/ask", headers=heads[who], json={"question": q}).json()
        retrieved = {x["doc_id"] for x in r.get("retrieved", [])}
        cited = {x["doc_id"] for x in r.get("citations", [])}
        ans = r.get("answer", "")
        refused = ans.startswith("I don't know")
        if exp:
            row = {"answerable": True, "hit": ids[exp] in retrieved, "cited_ok": ids[exp] in cited,
                   "fact_ok": fact.casefold() in ans.casefold(), "refused": refused}  # fmt: skip
        else:
            row = {"answerable": False, "forbidden_retrieved": ids[forbidden] in retrieved | cited,
                   "fact_leak": any(x in ans for x in ("97,000", "118,000", "6.4", "15 percent")),
                   "refused": refused}  # fmt: skip
        rows.append(
            {"persona": who, "question": q, "model": r.get("model"), "answer": ans[:300], **row}
        )
        print(json.dumps(rows[-1]))
    ans_rows = [r for r in rows if r["answerable"]]
    neg = [r for r in rows if not r["answerable"]]
    summary = {
        "questions": len(rows),
        "answerable": len(ans_rows),
        "hit_rate": round(sum(r["hit"] for r in ans_rows) / len(ans_rows), 3),
        "citation_ok": round(sum(r["cited_ok"] for r in ans_rows) / len(ans_rows), 3),
        "fact_correct": round(sum(r["fact_ok"] for r in ans_rows) / len(ans_rows), 3),
        "false_refusal": round(sum(r["refused"] for r in ans_rows) / len(ans_rows), 3),
        "must_refuse": len(neg),
        "forbidden_doc_retrieved": sum(r["forbidden_retrieved"] for r in neg),
        "forbidden_fact_in_answer": sum(r["fact_leak"] for r in neg),
        "refusal_ok": round(sum(r["refused"] for r in neg) / len(neg), 3),
        "models": sorted({r["model"] for r in rows if r["model"]}),
    }
    record("new_company_eval", {"company": "freshly registered (slug nova-eval-<run>)", "summary": summary,
                                "rows": rows})  # fmt: skip
    c.close()
    return (
        0
        if summary["forbidden_doc_retrieved"] == 0 and summary["forbidden_fact_in_answer"] == 0
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
