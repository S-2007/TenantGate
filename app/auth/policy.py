"""The access policy: ONE pure function, the single source of truth for "may this user read this doc".

Reused by the post-retrieval check (Phase 3), the eval review, and the tests. No I/O, no globals
that change at runtime, no exceptions for "no": every unexpected input is a deny.

Decision table (see docs/ACCESS_MODEL.md):

  inactive user                                   -> DENY
  user tenant != document tenant (or either unset) -> DENY   (admin included: admin is tenant-scoped)
  public        -> ALLOW any authenticated user of the same tenant
  internal      -> ALLOW if the user holds at least one EMPLOYEE role
  confidential  -> ALLOW if user roles ∩ allowed_roles ≠ ∅  OR  user id ∈ allowed_users
  anything else -> DENY  (unknown classification)

Admin gets no implicit content access: an admin reads a confidential document only when
``admin`` is in its ``allowed_roles`` (least privilege; admins manage users, not content).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

Classification = Literal["public", "internal", "confidential"]
CLASSIFICATIONS: tuple[str, ...] = ("public", "internal", "confidential")

# Roles that make someone an employee (internal documents). ``contractor`` is a known role that
# is deliberately NOT an employee role: it can read public documents of its tenant only.
EMPLOYEE_ROLES: frozenset[str] = frozenset(
    {"employee", "manager", "hr", "finance", "engineering", "admin"}
)
KNOWN_ROLES: frozenset[str] = EMPLOYEE_ROLES | {"contractor"}


def _as_uuid(value: object) -> uuid.UUID | None:
    """Parse a UUID from a UUID or string; None for anything else (callers then deny)."""
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def _roles(values: Iterable[object]) -> frozenset[str]:
    return frozenset(v for v in values if isinstance(v, str) and v)


@dataclass(frozen=True)
class AuthUser:
    """The authenticated principal. Built only from the DB after token verification.

    ``roles`` are the roles stored in Postgres at request time, never the token's copy.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID
    roles: frozenset[str] = field(default_factory=frozenset)
    is_active: bool = True

    def __post_init__(self) -> None:
        # Normalise so equality/hashing is stable and a list can't be mutated later.
        object.__setattr__(self, "roles", _roles(self.roles))


@dataclass(frozen=True)
class DocACL:
    """Access metadata of one document (or of a chunk, which carries a copy of it)."""

    doc_id: str
    tenant_id: uuid.UUID
    classification: str
    allowed_roles: frozenset[str] = field(default_factory=frozenset)
    allowed_users: frozenset[uuid.UUID] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_roles", _roles(self.allowed_roles))
        users = frozenset(u for u in (_as_uuid(x) for x in self.allowed_users) if u is not None)
        object.__setattr__(self, "allowed_users", users)


def can_access(user: AuthUser | None, doc: DocACL | None) -> bool:
    """True only if ``user`` may read ``doc``. Deny by default."""
    if user is None or doc is None:
        return False
    if user.is_active is not True:
        return False
    user_tenant, doc_tenant = _as_uuid(user.tenant_id), _as_uuid(doc.tenant_id)
    if user_tenant is None or doc_tenant is None or user_tenant != doc_tenant:
        return False
    roles = user.roles & KNOWN_ROLES  # unknown role strings grant nothing
    if doc.classification == "public":
        return True
    if doc.classification == "internal":
        return bool(roles & EMPLOYEE_ROLES)
    if doc.classification == "confidential":
        user_id = _as_uuid(user.id)
        return bool(roles & doc.allowed_roles) or (
            user_id is not None and user_id in doc.allowed_users
        )
    return False
