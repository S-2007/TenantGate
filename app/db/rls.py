"""Per-transaction identity for Postgres row-level security (layer 2 of 3).

The RLS policies (migration 0004) read ``app.tenant_id``, ``app.user_id`` and ``app.roles``
through the SQL helpers ``app_tenant_id()``, ``app_user_id()`` and ``app_roles()``. They are
set with ``set_config(name, value, true)``: transaction-local, so with Neon's transaction-mode
pooler a value can never survive into another request that reuses the server connection.

Fail closed: an inactive or missing user sets nothing, and with no settings every policy
evaluates to NULL, which means no rows.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.policy import KNOWN_ROLES, AuthUser, _as_uuid

_ROLE = re.compile(r"^[a-z_]+$")


def apply_rls_context(session: Session, user: AuthUser | None) -> bool:
    """Bind ``user`` to the current transaction. Returns False (and sets nothing) if not allowed."""
    if user is None or user.is_active is not True:
        return False
    tenant, user_id = _as_uuid(user.tenant_id), _as_uuid(user.id)
    if tenant is None or user_id is None:
        return False
    roles = sorted(r for r in user.roles & KNOWN_ROLES if _ROLE.match(r))
    session.execute(
        text(
            "SELECT set_config('app.tenant_id', :t, true), "
            "set_config('app.user_id', :u, true), "
            "set_config('app.roles', :r, true)"
        ),
        {"t": str(tenant), "u": str(user_id), "r": ",".join(roles)},
    )
    return True
