"""POST /auth/login (public) and GET /auth/me (authenticated)."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth.dependencies import client_ip, get_current_user, get_user_repository
from app.auth.passwords import MAX_PASSWORD_CHARS, dummy_hash, verify_password
from app.auth.policy import AuthUser
from app.auth.tokens import issue_access_token
from app.auth.users import UserRepository
from app.config import get_settings

security_log = logging.getLogger("app.security")
router = APIRouter(prefix="/auth", tags=["auth"])

GENERIC_LOGIN_ERROR = "invalid email or password"


class LoginRequest(BaseModel):
    """Credentials. Unknown fields are rejected (422)."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 token type, not a secret
    expires_in: int


class MeResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    roles: list[str]


@router.post("/login", response_model=TokenResponse)
def login(
    req: LoginRequest,
    request: Request,
    repo: UserRepository = Depends(get_user_repository),  # noqa: B008
) -> TokenResponse:
    """Exchange email + password for a short-lived access token.

    Unknown email, wrong password and deactivated account all get the same 401 with the same
    message, and all run exactly one Argon2 verification, so neither the body nor the timing
    says which case it was.
    """
    try:
        rec = repo.get_by_email(req.email)
    except ConnectionError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable") from exc
    password_ok = verify_password(rec.password_hash if rec else dummy_hash(), req.password)
    if rec is None or not password_ok or not rec.is_active:
        reason = (
            "unknown_email" if rec is None else ("bad_password" if not password_ok else "inactive")
        )
        security_log.warning(
            "login_failed reason=%s user=%s ip=%s",
            reason,
            rec.id if rec else "-",
            client_ip(request),
        )
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            GENERIC_LOGIN_ERROR,
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, ttl = issue_access_token(
        user_id=rec.id, tenant_id=rec.tenant_id, roles=rec.roles, settings=get_settings()
    )
    security_log.info("login_ok user=%s tenant=%s", rec.id, rec.tenant_id)
    return TokenResponse(access_token=token, expires_in=ttl)


@router.get("/me", response_model=MeResponse)
def me(user: AuthUser = Depends(get_current_user)) -> MeResponse:  # noqa: B008
    """The caller as the server sees it (roles from the database, not from the token)."""
    return MeResponse(id=user.id, tenant_id=user.tenant_id, roles=sorted(user.roles))
