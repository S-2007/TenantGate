"""Login roles for the running app: members of the low-privilege ``rag_app`` group role.

Migration 0002 creates ``rag_app`` (NOLOGIN) with SELECT-only grants. The app connects as a LOGIN
role that is a member of it and has no other attributes (no superuser, no CREATEROLE, no
BYPASSRLS, owns nothing); those attributes are asserted after every create/rotate.

Passwords: Neon's control plane manages role passwords and rejects pre-hashed SCRAM verifiers
("Neon only supports being given plaintext passwords"), so the plaintext goes in the statement.
It is random (256 bits), limited to URL-safe characters (checked before it is inlined, since DDL
cannot take bind parameters) and rotated by re-running scripts/create_app_role.py.
"""

from __future__ import annotations

import re
import secrets
from urllib.parse import quote, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.orm import Session

APP_GROUP_ROLE = "rag_app"
_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_SAFE_PASSWORD = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def new_password() -> str:
    """Random 43-char URL-safe password (256 bits)."""
    return secrets.token_urlsafe(32)


def _check_name(name: str) -> str:
    if not _ROLE_NAME.match(name) or name == APP_GROUP_ROLE:
        raise ValueError(f"invalid login role name {name!r}")
    return name


def upsert_login_role(session: Session, name: str, password: str) -> None:
    """Create or rotate ``name`` as a LOGIN member of rag_app (owner session required)."""
    name = _check_name(name)
    if not _SAFE_PASSWORD.match(password):  # it is inlined below: DDL takes no bind params
        raise ValueError("login role password must be 32-128 URL-safe characters")
    # SUPERUSER/REPLICATION/BYPASSRLS are left at their default (off): a non-superuser owner may
    # not even state them. They are asserted below instead.
    attrs = "LOGIN NOCREATEDB NOCREATEROLE INHERIT"
    exists = session.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": name}).first()
    verb = "ALTER" if exists else "CREATE"
    # exec_driver_sql: no SQLAlchemy bind-parameter parsing of the literal.
    session.connection().exec_driver_sql(f"{verb} ROLE {name} WITH {attrs} PASSWORD '{password}'")
    session.execute(text(f"GRANT {APP_GROUP_ROLE} TO {name}"))
    unsafe = session.execute(
        text(
            "SELECT rolsuper OR rolbypassrls OR rolreplication OR rolcreaterole OR rolcreatedb "
            "FROM pg_roles WHERE rolname = :n"
        ),
        {"n": name},
    ).scalar_one()
    if unsafe:
        raise RuntimeError(f"login role {name} has unsafe attributes; aborting (rolled back)")


def drop_login_role(session: Session, name: str) -> None:
    """Drop a login role if it exists (owner session required)."""
    name = _check_name(name)
    session.execute(text(f"DROP ROLE IF EXISTS {name}"))


def app_url(base_url: str, role: str, password: str) -> str:
    """``base_url`` (same host/db/options) with the credentials replaced by ``role:password``."""
    parts = urlsplit(base_url)
    host = parts.hostname or ""
    netloc = f"{quote(role, safe='')}:{quote(password, safe='')}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def role_privileges(session: Session) -> dict[str, object]:
    """Attributes of the *current* role, used to prove the app role is low-privilege."""
    row = session.execute(
        text(
            """
            SELECT r.rolname, r.rolsuper, r.rolbypassrls, r.rolcreaterole, r.rolcreatedb,
                   pg_has_role(current_user, :g, 'MEMBER') AS in_app_group,
                   (SELECT count(*) FROM pg_tables
                     WHERE schemaname = 'public' AND tableowner = current_user) AS owned_tables
            FROM pg_roles r WHERE r.rolname = current_user
            """
        ),
        {"g": APP_GROUP_ROLE},
    ).one()
    return {
        "role": row.rolname,
        "superuser": row.rolsuper,
        "bypass_rls": row.rolbypassrls,
        "create_role": row.rolcreaterole,
        "create_db": row.rolcreatedb,
        "in_app_group": row.in_app_group,
        "owned_tables": int(row.owned_tables),
    }
