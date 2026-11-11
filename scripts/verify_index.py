"""Scan the whole chunk index and exit non-zero if any chunk violates an access invariant.

python -m scripts.verify_index

Checks (see app/admin/verify.py): missing or unknown tenant, chunk without document, chunk
tenant differs from its document, chunk ACL (classification, roles, users, version) differs from
its document, invalid ACL shape, wrong embedding dimension, document granting a user of another
tenant. Read-only. Runs as the owner on DATABASE_URL_DIRECT so it sees every row.

Exit codes: 0 clean, 1 violations found, 2 could not run.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict

from app.admin.verify import verify_index
from app.db.session import admin_transaction, reset_admin_engine


def main() -> int:
    """Run the audit and print a JSON report."""
    try:
        with admin_transaction() as session:
            report = verify_index(session)  # SELECTs only; the commit on exit writes nothing
    except Exception as exc:  # noqa: BLE001 - report any failure as "could not verify"
        print(f"verify_index could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        reset_admin_engine()
    out = asdict(report)
    out["ok"] = report.ok
    print(json.dumps(out, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
