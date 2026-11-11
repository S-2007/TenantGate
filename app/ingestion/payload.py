"""``ChunkPayload``: the only shape a chunk may have on its way into the database.

Validation mirrors the database constraints (so a bad row fails in Python with a readable
message, before any write) and adds what a CHECK cannot express: the embedding dimension, finite
values, and known-role lists without duplicates. Every field is required; nothing is defaulted, so
a forgotten ACL is an error, not an empty list.
"""

from __future__ import annotations

import math
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.auth.policy import KNOWN_ROLES
from app.db.models import EMBEDDING_DIM


class ChunkPayload(BaseModel):
    """One validated chunk row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: uuid.UUID
    doc_id: str = Field(min_length=1)
    tenant_id: uuid.UUID
    title: str = Field(min_length=1)
    chunk_index: int = Field(ge=0)
    text: str = Field(min_length=1)
    embedding: tuple[float, ...]
    classification: Literal["public", "internal", "confidential"]
    allowed_roles: tuple[str, ...]
    allowed_users: tuple[uuid.UUID, ...]
    acl_version: int = Field(ge=1)

    @field_validator("text", "title", "doc_id")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @field_validator("embedding")
    @classmethod
    def _embedding_ok(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if len(v) != EMBEDDING_DIM:
            raise ValueError(f"embedding has {len(v)} dims, expected {EMBEDDING_DIM}")
        if not all(math.isfinite(x) for x in v):
            raise ValueError("embedding contains NaN or infinity")
        return v

    @field_validator("allowed_roles")
    @classmethod
    def _roles_ok(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        unknown = set(v) - KNOWN_ROLES
        if unknown:
            raise ValueError(f"unknown roles {sorted(unknown)}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate roles")
        return tuple(sorted(v))

    @field_validator("allowed_users")
    @classmethod
    def _users_ok(cls, v: tuple[uuid.UUID, ...]) -> tuple[uuid.UUID, ...]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate users")
        return tuple(sorted(v, key=str))

    @model_validator(mode="after")
    def _acl_shape(self) -> ChunkPayload:
        has_acl = bool(self.allowed_roles or self.allowed_users)
        if self.classification == "confidential" and not has_acl:
            raise ValueError("confidential chunk needs allowed_roles or allowed_users")
        if self.classification in ("public", "internal") and has_acl:
            raise ValueError(f"{self.classification} chunk must have empty allowed_roles/users")
        return self

    def row(self) -> dict:
        """Column dict for INSERT."""
        d = self.model_dump()
        d["embedding"] = list(self.embedding)
        d["allowed_roles"] = list(self.allowed_roles)
        d["allowed_users"] = list(self.allowed_users)
        return d
