"""Admin write endpoints: document ACLs, user roles, user activation (same tenant only).

PATCH /admin/documents/{doc_id}/acl   {classification?, allowed_roles?, allowed_users?}
PATCH /admin/users/{user_id}/roles    {roles}
PATCH /admin/users/{user_id}/status   {is_active}

- 403 unless the DB-confirmed caller is an admin. The database function re-checks this.
- 404 for a document or user of another tenant, identical to one that does not exist.
- Changes take effect on the caller's next query: the document, all its chunks and the
  ``acl_change`` audit row are written in one transaction (D-033).
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.admin.acl import AclChangeError, set_document_acl, set_user
from app.audit.api import require_admin
from app.auth.policy import KNOWN_ROLES, AuthUser

router = APIRouter(prefix="/admin", tags=["admin"])

RoleName = Literal[tuple(sorted(KNOWN_ROLES))]  # type: ignore[valid-type]


class DocumentAclPatch(BaseModel):
    """Fields to change; omitted fields keep their value. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    classification: Literal["public", "internal", "confidential"] | None = None
    allowed_roles: list[RoleName] | None = Field(default=None, max_length=len(KNOWN_ROLES))
    allowed_users: list[uuid.UUID] | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _something(self) -> DocumentAclPatch:
        if (
            self.classification is None
            and self.allowed_roles is None
            and self.allowed_users is None
        ):
            raise ValueError("send at least one of classification, allowed_roles, allowed_users")
        return self


class RolesPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roles: list[RoleName] = Field(min_length=1, max_length=len(KNOWN_ROLES))


class StatusPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool


def _http(exc: AclChangeError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


@router.patch("/documents/{doc_id}/acl")
def patch_document_acl(
    doc_id: str,
    body: DocumentAclPatch,
    request: Request,
    admin: AuthUser = Depends(require_admin),  # noqa: B008
) -> dict:
    """Atomically change a document's ACL (and all its chunks). Returns before/after."""
    if len(doc_id) > 200:
        raise HTTPException(404, "not found")
    try:
        return set_document_acl(
            admin,
            doc_id,
            classification=body.classification,
            allowed_roles=body.allowed_roles,
            allowed_users=body.allowed_users,
            request_id=request.state.request_id,
        )
    except AclChangeError as exc:
        raise _http(exc) from exc


@router.patch("/users/{user_id}/roles")
def patch_user_roles(
    user_id: uuid.UUID,
    body: RolesPatch,
    request: Request,
    admin: AuthUser = Depends(require_admin),  # noqa: B008
) -> dict:
    """Replace a user's roles. Effective on that user's next request (roles are read per request)."""
    try:
        return set_user(admin, user_id, roles=body.roles, request_id=request.state.request_id)
    except AclChangeError as exc:
        raise _http(exc) from exc


@router.patch("/users/{user_id}/status")
def patch_user_status(
    user_id: uuid.UUID,
    body: StatusPatch,
    request: Request,
    admin: AuthUser = Depends(require_admin),  # noqa: B008
) -> dict:
    """Deactivate or reactivate a user. A deactivated user's valid token fails on next use."""
    try:
        return set_user(
            admin, user_id, is_active=body.is_active, request_id=request.state.request_id
        )
    except AclChangeError as exc:
        raise _http(exc) from exc
