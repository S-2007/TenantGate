"""FastAPI dependencies: who is asking, derived only from a verified token + the database.

``get_current_user`` is the only way an endpoint learns the caller's identity. It never reads
tenant, roles or user id from the body, query string or headers (other than the bearer token).
"""

from __future__ import annotations

import logging
from functools import lru_cache

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.policy import AuthUser
from app.auth.tokens import TokenError, decode_access_token
from app.auth.users import SqlUserRepository, UserRepository
from app.config import get_settings

security_log = logging.getLogger("app.security")

_bearer = HTTPBearer(auto_error=False)
_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


@lru_cache(maxsize=1)
def _default_repo() -> SqlUserRepository:
    return SqlUserRepository()


def get_user_repository() -> UserRepository:
    """User store dependency (overridden in unit tests)."""
    return _default_repo()


def client_ip(request: Request) -> str:
    """Peer address for security logs (no proxy headers are trusted)."""
    return request.client.host if request.client else "?"


def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008
    repo: UserRepository = Depends(get_user_repository),  # noqa: B008
) -> AuthUser:
    """Verified, active, DB-confirmed caller. Any doubt -> 401 (one generic response)."""
    if creds is None or creds.scheme.lower() != "bearer":
        raise _UNAUTHORIZED
    try:
        claims = decode_access_token(creds.credentials, settings=get_settings())
    except TokenError as exc:
        security_log.warning(
            "auth_rejected reason=%s path=%s ip=%s",
            exc.reason,
            request.url.path,
            client_ip(request),
        )
        raise _UNAUTHORIZED from None
    try:
        rec = repo.get_by_id(claims.sub)
    except ConnectionError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable") from exc
    if rec is None or not rec.is_active:
        security_log.warning(
            "auth_rejected reason=%s user=%s path=%s",
            "unknown_user" if rec is None else "inactive_user",
            claims.sub,
            request.url.path,
        )
        raise _UNAUTHORIZED
    if rec.tenant_id != claims.tenant_id:
        security_log.warning(
            "auth_rejected reason=tenant_mismatch user=%s token_tenant=%s db_tenant=%s",
            rec.id,
            claims.tenant_id,
            rec.tenant_id,
        )
        raise _UNAUTHORIZED
    if set(claims.roles) != set(rec.roles):
        security_log.info("roles_changed_since_issuance user=%s (using database roles)", rec.id)
    return AuthUser(id=rec.id, tenant_id=rec.tenant_id, roles=frozenset(rec.roles), is_active=True)
