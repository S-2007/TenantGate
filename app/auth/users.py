"""User lookup for authentication. Postgres is the source of truth for identity and roles."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select

from app.db.models import User
from app.db.session import transaction


@dataclass(frozen=True)
class UserRecord:
    """What auth needs to know about a user."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    password_hash: str
    roles: tuple[str, ...]
    is_active: bool
    token_version: int = 1  # Phase 7: must equal the token's "tv" claim


class UserRepository(Protocol):
    """Read-only user store (swapped for an in-memory one in unit tests)."""

    def get_by_email(self, email: str) -> UserRecord | None: ...

    def get_by_id(self, user_id: uuid.UUID) -> UserRecord | None: ...


def _record(u: User) -> UserRecord:
    return UserRecord(
        id=u.id,
        tenant_id=u.tenant_id,
        email=u.email,
        password_hash=u.password_hash,
        roles=tuple(sorted(u.roles)),
        is_active=u.is_active,
        token_version=u.token_version,
    )


class SqlUserRepository:
    """Reads ``users`` through the app's low-privilege connection (one short transaction each)."""

    def get_by_email(self, email: str) -> UserRecord | None:
        """Exact match on the stored (lower-case) email."""
        with transaction() as s:
            u = s.scalars(select(User).where(User.email == email.strip().lower())).first()
            return _record(u) if u else None

    def get_by_id(self, user_id: uuid.UUID) -> UserRecord | None:
        """Primary-key lookup."""
        with transaction() as s:
            u = s.get(User, user_id)
            return _record(u) if u else None
