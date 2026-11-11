"""Seed tenants, users and documents from the corpus manifest (dev / test data only).

Two steps:
- ``build_plan(manifest)`` is pure: it turns the manifest into the exact rows to store, with
  deterministic UUIDs (uuid5) so every run and every branch gets the same ids.
- ``apply_plan(session, plan, password)`` upserts them idempotently: a second run changes
  nothing and reports everything as unchanged. A document whose ACL changes gets
  ``acl_version + 1``; that is what later lets chunks and caches detect stale permissions.

Never run against production: ``scripts/seed.py`` refuses when ENV=production and when the Neon
branch is the default/production branch.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.passwords import hash_password, verify_password
from app.db.models import Document, Tenant, User

# Fixed namespace: changing it would change every seeded id.
SEED_NAMESPACE = uuid.UUID("6b1d3c9e-2f4a-5e8b-9c7d-1a2b3c4d5e6f")
EMAIL_DOMAIN = "example"  # RFC 2606 reserved TLD: these addresses can never be real


class SeedError(RuntimeError):
    """The manifest cannot be seeded safely (e.g. it would move a row to another tenant)."""


def tenant_uuid(slug: str) -> uuid.UUID:
    """Deterministic tenant id."""
    return uuid.uuid5(SEED_NAMESPACE, f"tenant:{slug}")


def user_uuid(key: str) -> uuid.UUID:
    """Deterministic user id from the manifest key, e.g. ``acme:employee_1``."""
    return uuid.uuid5(SEED_NAMESPACE, f"user:{key}")


def email_for(key: str) -> str:
    """``acme:employee_1`` -> ``employee_1@acme.example``."""
    tenant, _, slot = key.partition(":")
    return f"{slot}@{tenant}.{EMAIL_DOMAIN}".lower()


@dataclass(frozen=True)
class TenantRow:
    id: uuid.UUID
    slug: str
    name: str


@dataclass(frozen=True)
class UserRow:
    id: uuid.UUID
    key: str
    tenant_id: uuid.UUID
    email: str
    roles: tuple[str, ...]
    is_active: bool


@dataclass(frozen=True)
class DocumentRow:
    id: str
    tenant_id: uuid.UUID
    title: str
    source_path: str
    classification: str
    allowed_roles: tuple[str, ...]
    allowed_users: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class SeedPlan:
    tenants: tuple[TenantRow, ...]
    users: tuple[UserRow, ...]
    documents: tuple[DocumentRow, ...]


def build_plan(manifest: dict) -> SeedPlan:
    """Rows to store, derived only from the manifest. Raises SeedError on unresolvable refs."""
    tenants = tuple(
        TenantRow(tenant_uuid(t["tenant_id"]), t["tenant_id"], t["name"])
        for t in manifest["tenants"]
    )
    users = tuple(
        UserRow(
            id=user_uuid(u["key"]),
            key=u["key"],
            tenant_id=tenant_uuid(u["tenant"]),
            email=email_for(u["key"]),
            roles=tuple(sorted(set(u["roles"]))),
            is_active=bool(u.get("active", True)),
        )
        for u in manifest["users"]
    )
    by_key = {u.key: u for u in users}
    docs = []
    for d in manifest["documents"]:
        user_ids = []
        for key in d.get("allowed_users", []):
            if key not in by_key:
                raise SeedError(f"{d['doc_id']}: unknown user {key}")
            if by_key[key].tenant_id != tenant_uuid(d["tenant"]):
                raise SeedError(f"{d['doc_id']}: user {key} belongs to another tenant")
            user_ids.append(by_key[key].id)
        docs.append(
            DocumentRow(
                id=d["doc_id"],
                tenant_id=tenant_uuid(d["tenant"]),
                title=d["title"],
                source_path=d["path"],
                classification=d["classification"],
                allowed_roles=tuple(sorted(set(d.get("allowed_roles", [])))),
                allowed_users=tuple(sorted(set(user_ids), key=str)),
            )
        )
    return SeedPlan(tenants, users, tuple(docs))


@dataclass
class Counts:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


@dataclass
class SeedStats:
    tenants: Counts = field(default_factory=Counts)
    users: Counts = field(default_factory=Counts)
    documents: Counts = field(default_factory=Counts)
    acl_version_bumps: int = 0
    passwords_reset: int = 0
    documents_not_in_manifest: list[str] = field(default_factory=list)


def _same_tenant_or_fail(kind: str, ident: object, have: uuid.UUID, want: uuid.UUID) -> None:
    if have != want:
        raise SeedError(f"{kind} {ident} already exists in another tenant; refusing to move it")


def apply_plan(session: Session, plan: SeedPlan, password: str) -> SeedStats:
    """Idempotent upsert of ``plan`` inside the caller's (owner) transaction."""
    stats = SeedStats()
    now = dt.datetime.now(dt.UTC)

    existing_t = {t.id: t for t in session.scalars(select(Tenant))}
    for t in plan.tenants:
        cur = existing_t.get(t.id)
        if cur is None:
            session.add(Tenant(id=t.id, slug=t.slug, name=t.name))
            stats.tenants.inserted += 1
        elif (cur.slug, cur.name) != (t.slug, t.name):
            cur.slug, cur.name = t.slug, t.name
            stats.tenants.updated += 1
        else:
            stats.tenants.unchanged += 1
    session.flush()

    existing_u = {u.id: u for u in session.scalars(select(User))}
    for u in plan.users:
        cur = existing_u.get(u.id)
        if cur is None:
            session.add(
                User(
                    id=u.id,
                    tenant_id=u.tenant_id,
                    email=u.email,
                    password_hash=hash_password(password),
                    roles=list(u.roles),
                    is_active=u.is_active,
                )
            )
            stats.users.inserted += 1
            continue
        _same_tenant_or_fail("user", u.key, cur.tenant_id, u.tenant_id)
        changed = False
        if (cur.email, tuple(sorted(cur.roles)), cur.is_active) != (u.email, u.roles, u.is_active):
            cur.email, cur.roles, cur.is_active = u.email, list(u.roles), u.is_active
            changed = True
        if not verify_password(cur.password_hash, password):
            cur.password_hash = hash_password(password)
            stats.passwords_reset += 1
            changed = True
        if changed:
            stats.users.updated += 1
        else:
            stats.users.unchanged += 1
    session.flush()

    existing_d = {d.id: d for d in session.scalars(select(Document))}
    for d in plan.documents:
        cur = existing_d.get(d.id)
        if cur is None:
            session.add(
                Document(
                    id=d.id,
                    tenant_id=d.tenant_id,
                    title=d.title,
                    source_path=d.source_path,
                    classification=d.classification,
                    allowed_roles=list(d.allowed_roles),
                    allowed_users=list(d.allowed_users),
                    acl_version=1,
                )
            )
            stats.documents.inserted += 1
            continue
        _same_tenant_or_fail("document", d.id, cur.tenant_id, d.tenant_id)
        acl_now = (
            cur.classification,
            tuple(sorted(cur.allowed_roles)),
            frozenset(cur.allowed_users),
        )
        acl_new = (d.classification, d.allowed_roles, frozenset(d.allowed_users))
        meta_changed = (cur.title, cur.source_path) != (d.title, d.source_path)
        if acl_now != acl_new:
            cur.classification = d.classification
            cur.allowed_roles = list(d.allowed_roles)
            cur.allowed_users = list(d.allowed_users)
            cur.acl_version = cur.acl_version + 1
            stats.acl_version_bumps += 1
        if acl_now != acl_new or meta_changed:
            cur.title, cur.source_path, cur.updated_at = d.title, d.source_path, now
            stats.documents.updated += 1
        else:
            stats.documents.unchanged += 1
    planned = {d.id for d in plan.documents}
    stats.documents_not_in_manifest = sorted(set(existing_d) - planned)
    session.flush()
    return stats


def seed_users_export(plan: SeedPlan, password: str) -> dict:
    """Content of data/seed_users.json (dev/test credentials; the file is git-ignored)."""
    return {
        "warning": "DEV/TEST ONLY. Never use these accounts or this password anywhere real.",
        "password": password,
        "users": [
            {
                "key": u.key,
                "id": str(u.id),
                "email": u.email,
                "roles": list(u.roles),
                "active": u.is_active,
            }
            for u in plan.users
        ],
    }


def plan_counts(plan: SeedPlan) -> dict[str, int]:
    """Row counts per table (used by the CLI and the idempotency test)."""
    return {
        "tenants": len(plan.tenants),
        "users": len(plan.users),
        "documents": len(plan.documents),
    }


def tenants_of(plan: SeedPlan) -> Sequence[str]:
    """Tenant slugs in the plan."""
    return [t.slug for t in plan.tenants]
