"""Create (or rotate) the app's low-privilege login role and point DATABASE_URL at it.

python -m scripts.create_app_role                 # role rag_app_login on the DATABASE_URL_DIRECT branch
python -m scripts.create_app_role --role my_name  # another login role name

What it does:
1. Refuses on the Neon default/production branch (Neon API guard, fails closed).
2. As the owner (direct URL), creates or rotates ``--role`` as a LOGIN member of ``rag_app``
   with a fresh random password, sent as a SCRAM verifier.
3. Rewrites the ``DATABASE_URL=`` line in .env to the *pooled* host of the same branch with the
   new role's credentials, then connects with it and checks the role is low-privilege.
Nothing secret is printed: only the role name, host and privilege check.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from app.config import Settings, get_settings, url_host

ENV_PATH = Path(".env")


def _endpoint(url: str) -> str:
    return url_host(url).split(".")[0].replace("-pooler", "")


def set_env_line(path: Path, name: str, value: str) -> None:
    """Replace ``NAME=...`` in an env file (quoted value), keeping every other line as-is."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    pat = re.compile(rf"^{re.escape(name)}=")
    out, done = [], False
    for line in lines:
        if pat.match(line) and not done:
            out.append(f'{name}="{value}"\n')
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f'{name}="{value}"\n')
    path.write_text("".join(out), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--role", default="rag_app_login")
    args = ap.parse_args(argv)

    s = get_settings()
    s.validate_for_migrations()
    s.require("database_url")
    if _endpoint(s.database_url) != _endpoint(s.database_url_direct):
        print("DATABASE_URL and DATABASE_URL_DIRECT point at different branches", file=sys.stderr)
        return 2

    from app.admin.roles import app_url, new_password, role_privileges, upsert_login_role
    from app.db.guard import assert_not_production
    from app.db.session import admin_transaction, build_engine, reset_admin_engine

    branch = assert_not_production(
        s.database_url_direct, neon_api_key=s.neon_api_key, neon_project_id=s.neon_project_id
    )
    password = new_password()
    with admin_transaction() as session:
        upsert_login_role(session, args.role, password)
    reset_admin_engine()

    new_url = app_url(s.database_url, args.role, password)
    # Verify before touching .env, so a failure leaves the old configuration in place.
    probe = build_engine(Settings(_env_file=None, database_url=new_url).sqlalchemy_url)
    from sqlalchemy.orm import Session

    with Session(probe) as session:
        privs = role_privileges(session)
    probe.dispose()
    unsafe = (
        privs["superuser"]
        or privs["bypass_rls"]
        or privs["owned_tables"]
        or not privs["in_app_group"]
    )
    if unsafe:
        print(f"Refusing: role is not low-privilege: {privs}", file=sys.stderr)
        return 1
    set_env_line(ENV_PATH, "DATABASE_URL", new_url)
    print(json.dumps({"branch": branch, "host": url_host(new_url), "privileges": privs}, indent=2))
    print("DATABASE_URL in .env now uses this role.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
