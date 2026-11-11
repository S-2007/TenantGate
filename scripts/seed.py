"""Seed step: validate the corpus, then create tenants, users and documents (dev/test only).

python -m scripts.seed                  # validate + seed the branch behind DATABASE_URL_DIRECT
python -m scripts.seed --validate-only  # Phase 1 behaviour: consistency checks, no database

Safety:
- Refuses to run when ENV=production (exit 2), before touching any database.
- Refuses when DATABASE_URL_DIRECT points at the Neon default/production branch (the guard
  resolves the branch through the Neon API and fails closed without NEON_API_KEY).
- The dev password is generated once and kept in data/seed_users.json (git-ignored), so
  re-running is idempotent: same rows, same counts, no password churn.
Exit code 1 on any validation problem, so ``make seed && make ingest`` stops early.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from app.config import get_settings
from app.ingestion.corpus import load_manifest, validate_manifest

SEED_USERS_PATH = Path("data/seed_users.json")


def check_canaries(corpus: Path, manifest: dict) -> list[str]:
    """Every canary must appear verbatim in its own doc and in no other tenant's docs."""
    reg_path = corpus / "canaries.json"
    if not reg_path.exists():
        return ["canaries.json missing"]
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    texts = {
        d["doc_id"]: (d["tenant"], (corpus / d["path"]).read_text(encoding="utf-8"))
        for d in manifest["documents"]
    }
    problems = []
    for c in reg["canaries"]:
        owner = texts.get(c["doc_id"])
        if owner is None:
            problems.append(f"canary {c['canary']!r}: unknown doc {c['doc_id']}")
            continue
        if c["canary"] not in owner[1]:
            problems.append(f"canary {c['canary']!r} not found in {c['doc_id']}")
        for did, (tenant, txt) in texts.items():
            if tenant != c["tenant"] and c["canary"] in txt:
                problems.append(f"canary {c['canary']!r} also appears in {did} (other tenant)")
    return problems


def dev_password(path: Path = SEED_USERS_PATH) -> str:
    """Reuse the stored dev password, or generate a new random one."""
    if path.exists():
        stored = json.loads(path.read_text(encoding="utf-8")).get("password")
        if stored:
            return str(stored)
    return secrets.token_urlsafe(18)


def main(argv: list[str] | None = None) -> int:
    """Validate, then (unless --validate-only) seed the database."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--validate-only", action="store_true")
    args = ap.parse_args(argv)

    s = get_settings()
    if s.env == "production" and not args.validate_only:
        print("Refusing to seed dev users: ENV=production", file=sys.stderr)
        return 2

    corpus = Path(s.corpus_path)
    manifest = load_manifest(corpus)
    problems = validate_manifest(manifest, corpus) + check_canaries(corpus, manifest)
    docs = manifest["documents"]
    summary: dict = {
        "tenants": [t["tenant_id"] for t in manifest["tenants"]],
        "users": len(manifest["users"]),
        "documents": len(docs),
        "by_tenant": dict(Counter(d["tenant"] for d in docs)),
        "by_classification": dict(Counter(d["classification"] for d in docs)),
        "user_specific_docs": sum(1 for d in docs if d.get("allowed_users")),
        "problems": problems,
    }
    if problems or args.validate_only:
        print(json.dumps(summary, indent=2))
        return 1 if problems else 0

    from app.admin.seed import apply_plan, build_plan, plan_counts, seed_users_export
    from app.db.guard import assert_not_production
    from app.db.session import admin_transaction, reset_admin_engine

    s.validate_for_migrations()
    branch = assert_not_production(
        s.database_url_direct, neon_api_key=s.neon_api_key, neon_project_id=s.neon_project_id
    )
    plan = build_plan(manifest)
    password = dev_password()
    with admin_transaction() as session:
        stats = apply_plan(session, plan, password)
    reset_admin_engine()
    SEED_USERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEED_USERS_PATH.write_text(
        json.dumps(seed_users_export(plan, password), indent=2) + "\n", encoding="utf-8"
    )
    summary.update({"branch": branch, "planned_rows": plan_counts(plan), "seed": asdict(stats)})
    summary["credentials_file"] = str(SEED_USERS_PATH)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
