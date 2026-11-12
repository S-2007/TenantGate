"""Permission changes: thin wrappers around the SECURITY DEFINER functions of migration 0006.

All authorization and all writes happen in ONE database transaction inside the function. The
document row and every one of its chunks change together with the audit row, or nothing
changes. This module only binds the caller's identity (RLS context) and maps SQLSTATEs.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.auth.policy import AuthUser
from app.db.rls import apply_rls_context
from app.db.session import transaction


class AclChangeError(Exception):
    """Base class; ``status`` is the HTTP status to return."""

    status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AclForbidden(AclChangeError):
    status = 403


class AclNotFound(AclChangeError):
    status = 404


class AclInvalid(AclChangeError):
    status = 422


class AclConflict(AclChangeError):
    """Phase 6: duplicate (23505) or a quota / cap reached (54000)."""

    status = 409


_SQLSTATE = {
    "42501": AclForbidden,
    "P0002": AclNotFound,
    "22023": AclInvalid,
    "23514": AclInvalid,
    "23505": AclConflict,
    "54000": AclConflict,
}
_PUBLIC_MESSAGES = {
    AclForbidden: "admin role required",
    AclNotFound: "not found",
    AclConflict: "already exists",
}


def _map(exc: DBAPIError, *, forbidden_detail: bool = False) -> AclChangeError:
    code = getattr(exc.orig, "sqlstate", None)
    cls = _SQLSTATE.get(code or "")
    if cls is None:
        raise exc  # unexpected: surfaces as a generic 500 (message never sent to the client)
    if cls is AclInvalid:
        # our own RAISE messages are safe to show; CHECK violations get a generic message
        diag = getattr(exc.orig, "diag", None)
        msg = getattr(diag, "message_primary", None) if code == "22023" else None
        return AclInvalid(msg or "the requested ACL is not valid")
    if code == "54000":  # our own quota message ("document quota reached for this company")
        diag = getattr(exc.orig, "diag", None)
        return AclConflict(getattr(diag, "message_primary", None) or "quota reached")
    if code == "42501" and forbidden_detail:  # Phase 6 functions say which permission is missing
        diag = getattr(exc.orig, "diag", None)
        msg = getattr(diag, "message_primary", None)
        return AclForbidden(msg if msg and msg != "not allowed" else _PUBLIC_MESSAGES[cls])
    return cls(_PUBLIC_MESSAGES[cls])


def call_definer(user: AuthUser | None, sql: str, params: dict) -> dict:
    """Run one SECURITY DEFINER function as ``user`` (None = no identity, e.g. registration).

    Phase 6 entry point shared by tenants/users/uploads; same SQLSTATE mapping as ACL changes.
    """
    try:
        with transaction() as s:
            if user is not None and not apply_rls_context(s, user):
                raise AclForbidden("not allowed")
            out = s.execute(text(sql), params).scalar_one()
    except DBAPIError as exc:
        raise _map(exc, forbidden_detail=True) from exc
    return out if isinstance(out, dict) else json.loads(out)


def _call(admin: AuthUser, sql: str, params: dict) -> dict:
    try:
        with transaction() as s:
            if not apply_rls_context(s, admin):
                raise AclForbidden("admin role required")
            out = s.execute(text(sql), params).scalar_one()
    except DBAPIError as exc:
        raise _map(exc) from exc
    return out if isinstance(out, dict) else json.loads(out)


def set_document_acl(
    admin: AuthUser,
    doc_id: str,
    *,
    classification: str | None = None,
    allowed_roles: Sequence[str] | None = None,
    allowed_users: Sequence[uuid.UUID] | None = None,
    request_id: str,
) -> dict:
    """Change a document's ACL and every chunk's copy of it, atomically. Returns before/after."""
    return _call(
        admin,
        "SELECT admin_set_document_acl(:doc, CAST(:cls AS doc_classification), "
        "CAST(:roles AS text[]), CAST(:users AS uuid[]), CAST(:rid AS uuid))",
        {
            "doc": doc_id,
            "cls": classification,
            "roles": list(allowed_roles) if allowed_roles is not None else None,
            "users": [str(u) for u in allowed_users] if allowed_users is not None else None,
            "rid": request_id,
        },
    )


def set_user(
    admin: AuthUser,
    user_id: uuid.UUID,
    *,
    roles: Sequence[str] | None = None,
    is_active: bool | None = None,
    request_id: str,
) -> dict:
    """Change a user's roles and/or active flag (same tenant only). Returns before/after."""
    return _call(
        admin,
        "SELECT admin_set_user(CAST(:u AS uuid), CAST(:roles AS text[]), :active, "
        "CAST(:rid AS uuid))",
        {
            "u": str(user_id),
            "roles": list(roles) if roles is not None else None,
            "active": is_active,
            "rid": request_id,
        },
    )
