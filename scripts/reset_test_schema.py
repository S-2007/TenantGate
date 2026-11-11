"""Drop and recreate the public schema on the TEST branch so migrations run on an empty DB.

Guarded: refuses unless the TEST_DATABASE_URL_DIRECT branch is verified non-default via the
Neon API, and requires --yes. Never touches DATABASE_URL / DATABASE_URL_DIRECT.
"""

from __future__ import annotations

import argparse
import sys

from app.config import get_settings, normalize_pg_url
from app.db.guard import assert_not_production
from app.db.session import _libpq_dsn, connect_with_retry


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true", help="confirm destructive reset")
    args = ap.parse_args(argv)
    s = get_settings()
    s.require("test_database_url_direct")
    branch = assert_not_production(
        s.test_database_url_direct, neon_api_key=s.neon_api_key, neon_project_id=s.neon_project_id
    )
    if not args.yes:
        print(f"Would reset schema 'public' on Neon branch '{branch}'. Re-run with --yes.")
        return 1
    url = normalize_pg_url(s.test_database_url_direct, var_name="TEST_DATABASE_URL_DIRECT")
    with connect_with_retry(_libpq_dsn(url), stats=None) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        conn.commit()
    print(f"Reset schema 'public' on Neon branch '{branch}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
