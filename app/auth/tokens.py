"""Access tokens: short-lived HS256 JWTs (PyJWT). See docs/DECISIONS.md D-021 for RS256.

Issued claims: sub (user id), tenant_id, roles, tv (token version, Phase 7), iat, exp, jti, iss,
aud. A token whose ``tv`` differs from the user's current ``token_version`` is rejected, which
is how "log out everywhere", password changes and admin session revocation work. ``roles`` is
informational only: authorization always re-reads roles from Postgres (D-022).

Verification is strict and fails closed:
- the header ``alg`` must be in ALLOWED_ALGORITHMS *and* equal JWT_ALGORITHM; ``none`` and any
  other value are rejected before the signature is even considered;
- signature, exp, iss and aud are verified; every claim above is required;
- ``iat`` may not be in the future (beyond a small clock-skew leeway);
- sub / tenant_id must be UUIDs, roles a list of strings.
Every failure raises TokenError with a short machine-readable reason (for logs, never clients).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import jwt

from app.config import Settings

ALLOWED_ALGORITHMS = frozenset({"HS256"})
REQUIRED_CLAIMS = ("sub", "tenant_id", "roles", "tv", "iat", "exp", "jti", "iss", "aud")
LEEWAY_S = 10


class TokenError(Exception):
    """Token rejected. ``reason`` is for server logs; clients only ever see a generic 401."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class TokenClaims:
    """Verified claims."""

    sub: uuid.UUID
    tenant_id: uuid.UUID
    roles: tuple[str, ...]
    jti: str
    iat: int
    exp: int
    tv: int = 1


def issue_access_token(
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    roles: tuple[str, ...] | list[str],
    settings: Settings,
    token_version: int = 1,
    now: float | None = None,
) -> tuple[str, int]:
    """Signed token and its lifetime in seconds."""
    if settings.jwt_algorithm not in ALLOWED_ALGORITHMS:
        raise TokenError("algorithm_not_allowed")
    iat = int(now if now is not None else time.time())
    lifetime = settings.jwt_expire_minutes * 60
    claims = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "roles": sorted(set(roles)),
        "tv": int(token_version),
        "iat": iat,
        "exp": iat + lifetime,
        "jti": str(uuid.uuid4()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, lifetime


def decode_access_token(token: str, *, settings: Settings) -> TokenClaims:
    """Verify ``token`` and return its claims, or raise TokenError."""
    if not token or len(token) > 4096:
        raise TokenError("malformed")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise TokenError("malformed") from exc
    alg = header.get("alg")
    if alg not in ALLOWED_ALGORITHMS or alg != settings.jwt_algorithm:
        raise TokenError("algorithm_not_allowed")
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=LEEWAY_S,
            options={"require": list(REQUIRED_CLAIMS)},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise TokenError("wrong_audience") from exc
    except jwt.InvalidIssuerError as exc:
        raise TokenError("wrong_issuer") from exc
    except jwt.InvalidSignatureError as exc:
        raise TokenError("bad_signature") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise TokenError("missing_claim") from exc
    except jwt.ImmatureSignatureError as exc:
        raise TokenError("not_yet_valid") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("invalid") from exc

    iat = payload["iat"]
    if not isinstance(iat, int | float) or iat > time.time() + LEEWAY_S:
        raise TokenError("issued_in_future")
    roles = payload["roles"]
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise TokenError("invalid_claims")
    tv = payload["tv"]
    if not isinstance(tv, int) or isinstance(tv, bool) or tv < 1:
        raise TokenError("invalid_claims")
    try:
        sub = uuid.UUID(str(payload["sub"]))
        tenant_id = uuid.UUID(str(payload["tenant_id"]))
    except ValueError as exc:
        raise TokenError("invalid_claims") from exc
    return TokenClaims(
        sub=sub,
        tenant_id=tenant_id,
        roles=tuple(roles),
        jti=str(payload["jti"]),
        iat=int(iat),
        exp=int(payload["exp"]),
        tv=tv,
    )
