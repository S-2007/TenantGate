"""The ACL predicate that goes INSIDE the vector query (layer 1 of 3).

``build_acl_clause(user)`` returns a SQLAlchemy boolean expression over ``chunks`` that is exactly
equivalent to ``app.auth.policy.can_access`` evaluated on each chunk's ACL columns:

    tenant_id = :tenant
    AND (   visibility = 'tenant'                                   -- public
         OR (visibility = 'employees' AND :is_employee)             -- internal
         OR (visibility = 'restricted'                              -- confidential
             AND (allowed_roles && :roles OR :user_id = ANY(allowed_users))))

Every value is a bound parameter; nothing from the user is concatenated into SQL. The coarse
``visibility`` column (generated from ``classification`` at write time, migration 0003) keeps
the predicate flat and index-friendly instead of re-deriving the rule from several columns.
An inactive user, a missing user or a malformed tenant id gets ``FALSE`` (no rows), as in
``can_access``. Equivalence is proven against the seed data by tests/integration/test_retrieval_security.py.
"""

from __future__ import annotations

from sqlalchemy import Boolean, ColumnElement, Text, and_, any_, bindparam, false, or_
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from app.auth.policy import EMPLOYEE_ROLES, KNOWN_ROLES, AuthUser, _as_uuid
from app.db.models import Chunk


def build_acl_clause(user: AuthUser | None) -> ColumnElement[bool]:
    """WHERE predicate for ``chunks`` that admits exactly the rows ``can_access`` allows."""
    if user is None or user.is_active is not True:
        return false()
    tenant = _as_uuid(user.tenant_id)
    if tenant is None:
        return false()
    roles = sorted(user.roles & KNOWN_ROLES)  # unknown role strings grant nothing
    is_employee = bool(set(roles) & EMPLOYEE_ROLES)
    user_id = _as_uuid(user.id)

    confidential_match = Chunk.allowed_roles.overlap(
        bindparam("acl_roles", roles, type_=ARRAY(Text))
    )
    if user_id is not None:
        confidential_match = or_(
            confidential_match,
            bindparam("acl_user_id", user_id, type_=UUID(as_uuid=True))
            == any_(Chunk.allowed_users),
        )
    return and_(
        Chunk.tenant_id == bindparam("acl_tenant", tenant, type_=UUID(as_uuid=True)),
        or_(
            Chunk.visibility == "tenant",
            and_(
                Chunk.visibility == "employees",
                bindparam("acl_is_employee", is_employee, type_=Boolean),
            ),
            and_(Chunk.visibility == "restricted", confidential_match),
        ),
    )
