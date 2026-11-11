"""ORM models. The migrations are authoritative for constraints; these mirror the columns.

Phase 2: tenants, users, documents (the ACL source of truth) and ACL-carrying chunks.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID  # postgres ARRAY: supports && (overlap)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Must equal EMBEDDING_DIM in config and the constant in migration 0001.
# tests/unit/test_config.py asserts all three agree.
EMBEDDING_DIM = 384

# Postgres enum type created in migration 0002.
classification_enum = Enum(
    "public", "internal", "confidential", name="doc_classification", create_type=False
)


class Base(DeclarativeBase):
    """Declarative base for all models."""


class Tenant(Base):
    """A customer organisation. Every user, document and chunk belongs to exactly one."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class User(Base):
    """A login identity, scoped to one tenant. ``roles`` here are the source of truth."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Document(Base):
    """One source document and its ACL (the ACL source of truth for every chunk)."""

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="uq_documents_id_tenant"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # = doc_id
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    classification: Mapped[str] = mapped_column(classification_enum, nullable=False)
    allowed_roles: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    allowed_users: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False
    )
    acl_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Chunk(Base):
    """One retrievable chunk plus its embedding and a copy of its document's ACL."""

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("doc_id", "chunk_index"),
        ForeignKeyConstraint(
            ["doc_id", "tenant_id"],
            ["documents.id", "documents.tenant_id"],
            name="fk_chunks_document_same_tenant",
            ondelete="CASCADE",
        ),
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    doc_id: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    classification: Mapped[str] = mapped_column(classification_enum, nullable=False)
    visibility: Mapped[str] = mapped_column(
        Text,
        Computed(
            "CASE classification WHEN 'public' THEN 'tenant' "
            "WHEN 'internal' THEN 'employees' ELSE 'restricted' END",
            persisted=True,
        ),
    )
    allowed_roles: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    allowed_users: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False
    )
    acl_version: Mapped[int] = mapped_column(Integer, nullable=False)
