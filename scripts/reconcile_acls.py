"""Check (default) or repair chunk ACLs that drifted from their document.

python -m scripts.reconcile_acls          # check: exit 1 if any chunk differs (CI integrity gate)
python -m scripts.reconcile_acls --fix    # repair: copy each document's ACL onto its chunks

Runs as the owner over DATABASE_URL_DIRECT. Use ``--test`` to target TEST_DATABASE_URL_DIRECT
(guarded: never the production branch). Exit codes: 0 clean or repaired, 1 drift found by
--check, 2 could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from app.config import get_settings


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--test", action="store_true", help="use the TEST branch URLs")
    args = ap.parse_args(argv)

    s = get_settings()
    if args.test:
        from app.db.guard import assert_not_production

        s.require("test_database_url_direct")
        assert_not_production(
            s.test_database_url_direct,
            neon_api_key=s.neon_api_key,
            neon_project_id=s.neon_project_id,
        )
        os.environ["DATABASE_URL_DIRECT"] = s.test_database_url_direct
        get_settings.cache_clear()

    from app.admin.reconcile import reconcile
    from app.db.session import admin_transaction, reset_admin_engine

    try:
        with admin_transaction() as session:
            rep = reconcile(session, fix=args.fix)
    except Exception as exc:  # noqa: BLE001 - report any failure as "could not run"
        print(f"reconcile_acls could not run: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        reset_admin_engine()
    print(
        json.dumps(
            {"drifted": len(rep.drifted), "fixed": rep.fixed, "sample": rep.drifted[:10]},
            indent=2,
        )
    )
    if args.fix:
        return 0
    return 0 if rep.clean else 1


if __name__ == "__main__":
    sys.exit(main())
