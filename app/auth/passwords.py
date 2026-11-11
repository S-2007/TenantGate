"""Password hashing with Argon2id (argon2-cffi defaults, RFC 9106 low-memory profile).

``verify_password`` never raises for a wrong or malformed hash: it returns False, so callers have
exactly one failure path. ``DUMMY_HASH`` lets login spend the same verification time when the
email does not exist, so response time does not reveal which emails are registered.
"""

from __future__ import annotations

from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MAX_PASSWORD_CHARS = 1024  # bound the work an attacker can make us do per request

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Argon2id hash (includes salt and parameters)."""
    if not password or len(password) > MAX_PASSWORD_CHARS:
        raise ValueError("password must be 1-1024 characters")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True iff ``password`` matches. Wrong password, bad hash or oversize input -> False."""
    if not password or len(password) > MAX_PASSWORD_CHARS:
        password = "\x00invalid"  # noqa: S105 - sentinel that never matches; keeps timing uniform
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """A valid hash of a random value, verified when the user does not exist."""
    import secrets

    return _hasher.hash(secrets.token_urlsafe(32))
