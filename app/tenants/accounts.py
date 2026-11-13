"""Invites, sessions and the security dashboard (Phase 7).

Admin (same tenant only, re-checked by the database):
    POST   /admin/invites                    {email, roles} -> one-time invite link
    GET    /admin/invites                    pending / accepted / expired / revoked
    DELETE /admin/invites/{invite_id}
    POST   /admin/users/{user_id}/revoke-sessions
    GET    /admin/security/summary?days=7    tenant-scoped audit aggregates

Public / self:
    POST /auth/invite-preview   {token}            -> email + company (or one uniform 410)
    POST /auth/accept-invite    {token, password}  -> account created + signed in
    POST /auth/logout-all                          -> every token of mine stops working
    POST /auth/change-password  {current_password, new_password} -> new token; others die

Invite tokens: 256 random bits (``secrets.token_urlsafe(32)``), sent only in the link's
fragment (``/#invite=...``), which browsers never send to servers or in Referer headers. The
database stores only their SHA-256, so a database leak doesn't reveal usable invites. Preview and
accept are POSTs (the token never appears in a URL the server logs) and are rate-limited.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.admin.acl import AclChangeError, call_definer
from app.audit.api import require_admin
from app.auth.dependencies import get_current_user, get_user_repository
from app.auth.passwords import MAX_PASSWORD_CHARS, hash_password, verify_password
from app.auth.policy import AuthUser
from app.auth.tokens import issue_access_token
from app.auth.users import UserRepository
from app.config import get_settings
from app.db.rls import apply_rls_context
from app.db.session import transaction
from app.ratelimit import enforce
from app.tenants.api import ASSIGNABLE_ROLES, AssignableRole, _email, _password

security_log = logging.getLogger("app.security")
router = APIRouter(tags=["accounts"])

TOKEN_RE_LEN = (40, 64)  # token_urlsafe(32) is 43 chars


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _http(exc: AclChangeError, **messages: str) -> HTTPException:
    msg = messages.get(str(exc.status), exc.message)
    return HTTPException(exc.status, msg)


# ------------------------------------------------------------------ admin: invites
class InviteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=6, max_length=254)
    roles: list[AssignableRole] = Field(min_length=1, max_length=len(ASSIGNABLE_ROLES))

    _em = field_validator("email")(classmethod(lambda cls, v: _email(v)))


class InviteCreated(BaseModel):
    invite_id: uuid.UUID
    email: str
    roles: list[str]
    expires_at: dt.datetime
    invite_url: str  # shown once; the server keeps only its hash


class InviteOut(BaseModel):
    invite_id: uuid.UUID
    email: str
    roles: list[str]
    created_at: dt.datetime
    expires_at: dt.datetime
    status: str


def _ui_base(request: Request) -> str:
    origin = get_settings().ui_origin.strip()
    return origin.rstrip("/") if origin else str(request.base_url).rstrip("/")


@router.post("/admin/invites", response_model=InviteCreated, status_code=201)
async def create_invite(
    body: InviteCreate,
    request: Request,
    admin: AuthUser = Depends(require_admin),  # noqa: B008
) -> InviteCreated:
    """Invite a person by email. The admin never sets or sees their password."""
    s = get_settings()
    token = secrets.token_urlsafe(32)
    try:
        out = await run_in_threadpool(
            call_definer, admin,
            "SELECT admin_create_invite(:email, CAST(:roles AS text[]), :h, :ttl, :cap, "
            "CAST(:rid AS uuid))",
            {"email": body.email, "roles": list(body.roles), "h": token_hash(token),
             "ttl": s.invite_ttl_hours * 60, "cap": s.max_users_per_tenant,
             "rid": request.state.request_id},
        )  # fmt: skip
    except AclChangeError as exc:
        raise _http(exc, **{"409": exc.message if exc.message != "already exists"
                            else "a user with this email already exists"}) from exc  # fmt: skip
    return InviteCreated(
        invite_id=out["invite_id"], email=out["email"], roles=out["roles"],
        expires_at=out["expires_at"], invite_url=f"{_ui_base(request)}/#invite={token}",
    )  # fmt: skip


@router.get("/admin/invites", response_model=list[InviteOut])
def list_invites(admin: AuthUser = Depends(require_admin)) -> list[InviteOut]:  # noqa: B008
    from sqlalchemy import text

    with transaction() as s:
        if not apply_rls_context(s, admin):
            return []
        rows = s.execute(text("SELECT * FROM admin_list_invites()")).mappings().all()
    return [InviteOut(**r) for r in rows]


@router.delete("/admin/invites/{invite_id}")
def revoke_invite(
    invite_id: uuid.UUID,
    request: Request,
    admin: AuthUser = Depends(require_admin),  # noqa: B008
) -> dict:
    try:
        return call_definer(admin, "SELECT admin_revoke_invite(CAST(:i AS uuid), CAST(:rid AS uuid))",
                            {"i": str(invite_id), "rid": request.state.request_id})  # fmt: skip
    except AclChangeError as exc:
        raise _http(exc) from exc


@router.post("/admin/users/{user_id}/revoke-sessions")
def revoke_sessions(
    user_id: uuid.UUID,
    request: Request,
    admin: AuthUser = Depends(require_admin),  # noqa: B008
) -> dict:
    """Every token that user holds stops working on its next request."""
    try:
        return call_definer(admin, "SELECT admin_revoke_sessions(CAST(:u AS uuid), CAST(:rid AS uuid))",
                            {"u": str(user_id), "rid": request.state.request_id})  # fmt: skip
    except AclChangeError as exc:
        raise _http(exc) from exc


# ------------------------------------------------------------------ public: accept an invite
class InviteToken(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=TOKEN_RE_LEN[0], max_length=TOKEN_RE_LEN[1])


class InviteAccept(InviteToken):
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)

    _pw = field_validator("password")(classmethod(lambda cls, v: _password(v)))


class InvitePreview(BaseModel):
    email: str
    company: str
    roles: list[str]
    expires_at: dt.datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 token type
    expires_in: int


@router.post("/auth/invite-preview", response_model=InvitePreview)
def invite_preview(body: InviteToken, request: Request) -> InvitePreview:
    enforce(request, "invite", get_settings().invite_accept_rate_per_hour, 3600)
    try:
        return InvitePreview(**call_definer(None, "SELECT invite_preview(:h)",
                                            {"h": token_hash(body.token)}))  # fmt: skip
    except AclChangeError as exc:
        raise _http(exc) from exc


@router.post("/auth/accept-invite", response_model=TokenOut, status_code=201)
async def accept_invite(body: InviteAccept, request: Request) -> TokenOut:
    """Create the invited account with the person's own password, and sign them in."""
    s = get_settings()
    enforce(request, "invite", s.invite_accept_rate_per_hour, 3600)
    pw_hash = await run_in_threadpool(hash_password, body.password)
    try:
        out = await run_in_threadpool(
            call_definer, None,
            "SELECT accept_invite(:h, :pw, :cap, CAST(:rid AS uuid))",
            {"h": token_hash(body.token), "pw": pw_hash, "cap": s.max_users_per_tenant,
             "rid": request.state.request_id},
        )  # fmt: skip
    except AclChangeError as exc:
        if exc.status == 410:
            security_log.warning("invite_rejected ip=%s",
                                 request.client.host if request.client else "?")  # fmt: skip
        raise _http(exc, **{"409": "a user with this email already exists"}) from exc
    token, ttl = issue_access_token(user_id=uuid.UUID(out["user_id"]),
                                    tenant_id=uuid.UUID(out["tenant_id"]), roles=out["roles"],
                                    settings=s, token_version=out["token_version"])  # fmt: skip
    return TokenOut(access_token=token, expires_in=ttl)


# ------------------------------------------------------------------ self: sessions, password
@router.post("/auth/logout-all")
def logout_all(request: Request, user: AuthUser = Depends(get_current_user)) -> dict:  # noqa: B008
    """Revoke every token of the caller, including this one."""
    try:
        out = call_definer(user, "SELECT bump_my_token_version(NULL, CAST(:rid AS uuid))",
                           {"rid": request.state.request_id})  # fmt: skip
    except AclChangeError as exc:
        raise _http(exc) from exc
    return {"revoked": True, "token_version": out["token_version"]}


class PasswordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)

    _pw = field_validator("new_password")(classmethod(lambda cls, v: _password(v)))


@router.post("/auth/change-password", response_model=TokenOut)
async def change_password(
    body: PasswordChange,
    request: Request,
    user: AuthUser = Depends(get_current_user),  # noqa: B008
    repo: UserRepository = Depends(get_user_repository),  # noqa: B008
) -> TokenOut:
    """Verify the current password, set the new one, end every other session."""
    s = get_settings()
    enforce(request, "password", s.password_change_rate_per_hour, 3600)
    rec = await run_in_threadpool(repo.get_by_id, user.id)
    ok = rec is not None and await run_in_threadpool(verify_password, rec.password_hash,
                                                     body.current_password)  # fmt: skip
    if not ok:
        security_log.warning("password_change_rejected user=%s", user.id)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "current password is not correct")
    if body.new_password == body.current_password:
        raise HTTPException(422, "the new password must differ from the current one")
    new_hash = await run_in_threadpool(hash_password, body.new_password)
    try:
        out = await run_in_threadpool(
            call_definer, user, "SELECT bump_my_token_version(:pw, CAST(:rid AS uuid))",
            {"pw": new_hash, "rid": request.state.request_id},
        )  # fmt: skip
    except AclChangeError as exc:
        raise _http(exc) from exc
    token, ttl = issue_access_token(user_id=user.id, tenant_id=user.tenant_id,
                                    roles=sorted(user.roles), settings=s,
                                    token_version=out["token_version"])  # fmt: skip
    return TokenOut(access_token=token, expires_in=ttl)


# ------------------------------------------------------------------ admin: security dashboard
@router.get("/admin/security/summary")
def security_summary(
    days: int = Query(default=7, ge=1, le=90),
    admin: AuthUser = Depends(require_admin),  # noqa: B008
) -> dict:
    """Tenant-scoped aggregates of the audit log (computed by the database, admin re-checked).

    Titles of confidential documents appear only when the admin may read them; otherwise the
    document id alone (the same information the per-document audit endpoint gives).
    """
    try:
        return call_definer(admin, "SELECT security_summary(:d)", {"d": days})
    except AclChangeError as exc:
        raise _http(exc) from exc


# ------------------------------------------------------------------ UI: own workspace
class WorkspaceOut(BaseModel):
    tenant_id: uuid.UUID
    name: str
    slug: str


@router.get("/auth/workspace", response_model=WorkspaceOut)
def my_workspace(user: AuthUser = Depends(get_current_user)) -> WorkspaceOut:  # noqa: B008
    """The caller's OWN company (name for the UI). The tenant comes from the verified identity."""
    from sqlalchemy import select

    from app.db.models import Tenant

    with transaction() as s:
        row = s.execute(
            select(Tenant.id, Tenant.name, Tenant.slug).where(Tenant.id == user.tenant_id)
        ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return WorkspaceOut(tenant_id=row.id, name=row.name, slug=row.slug)
