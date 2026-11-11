"""In-memory user store and token helpers for auth unit tests (no database)."""

from __future__ import annotations

import uuid
from dataclasses import replace

from app.auth.passwords import hash_password
from app.auth.tokens import issue_access_token
from app.auth.users import UserRecord
from app.config import get_settings

PASSWORD = "correct horse battery staple"  # noqa: S105 - test-only credential
T1, T2 = uuid.UUID(int=1), uuid.UUID(int=2)
_HASH = hash_password(PASSWORD)  # hash once: Argon2 is slow on purpose


def make_user(**over) -> UserRecord:  # type: ignore[no-untyped-def]
    base = UserRecord(
        id=uuid.uuid4(),
        tenant_id=T1,
        email="employee_1@acme.example",
        password_hash=_HASH,
        roles=("employee",),
        is_active=True,
    )
    return replace(base, **over)


class MemoryRepo:
    """UserRepository backed by a dict; counts lookups."""

    def __init__(self, *users: UserRecord) -> None:
        self.users = {u.id: u for u in users}
        self.calls = 0

    def put(self, user: UserRecord) -> None:
        self.users[user.id] = user

    def get_by_email(self, email: str) -> UserRecord | None:
        self.calls += 1
        return next((u for u in self.users.values() if u.email == email.strip().lower()), None)

    def get_by_id(self, user_id: uuid.UUID) -> UserRecord | None:
        self.calls += 1
        return self.users.get(user_id)


def token_for(user: UserRecord, **kw) -> str:  # type: ignore[no-untyped-def]
    tok, _ = issue_access_token(
        user_id=user.id, tenant_id=user.tenant_id, roles=user.roles, settings=get_settings(), **kw
    )
    return tok


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
