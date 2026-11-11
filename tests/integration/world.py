"""A tiny two-tenant world for Neon integration tests, seeded with the production seed code.

Tenants ``itest-a`` and ``itest-b`` never collide with the real corpus tenants. ``cleanup``
removes every row they own (chunks cascade from documents).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.admin.seed import SeedPlan, apply_plan, build_plan, email_for, tenant_uuid, user_uuid
from app.db.models import Chunk, Document, Tenant, User
from app.ingestion.corpus import DocumentRecord

PASSWORD = "itest-password-not-secret-7f3a"  # noqa: S105 - throwaway test credential
TENANTS = ("itest-a", "itest-b")

MANIFEST = {
    "tenants": [
        {"tenant_id": "itest-a", "name": "Itest A"},
        {"tenant_id": "itest-b", "name": "Itest B"},
    ],
    "users": [
        {"key": "itest-a:employee_1", "tenant": "itest-a", "roles": ["employee"], "active": True},
        {"key": "itest-a:hr_1", "tenant": "itest-a", "roles": ["hr"], "active": True},
        {
            "key": "itest-a:multi_1",
            "tenant": "itest-a",
            "roles": ["finance", "manager"],
            "active": True,
        },
        {
            "key": "itest-a:contractor_1",
            "tenant": "itest-a",
            "roles": ["contractor"],
            "active": True,
        },
        {"key": "itest-a:off_1", "tenant": "itest-a", "roles": ["employee"], "active": False},
        {"key": "itest-a:admin_1", "tenant": "itest-a", "roles": ["admin"], "active": True},
        {"key": "itest-b:employee_1", "tenant": "itest-b", "roles": ["employee"], "active": True},
        {"key": "itest-b:admin_1", "tenant": "itest-b", "roles": ["admin"], "active": True},
    ],
    "documents": [
        {
            "doc_id": "itest-a-leave",
            "tenant": "itest-a",
            "title": "Itest A Leave Policy",
            "path": "itest-a/leave.md",
            "classification": "internal",
            "allowed_roles": [],
            "allowed_users": [],
        },
        {
            "doc_id": "itest-a-salary",
            "tenant": "itest-a",
            "title": "Itest A Salary Bands",
            "path": "itest-a/salary.md",
            "classification": "confidential",
            "allowed_roles": ["hr"],
            "allowed_users": [],
        },
        {
            "doc_id": "itest-a-review",
            "tenant": "itest-a",
            "title": "Itest A Performance Review",
            "path": "itest-a/review.md",
            "classification": "confidential",
            "allowed_roles": [],
            "allowed_users": ["itest-a:employee_1"],
        },
        {
            "doc_id": "itest-a-handbook",
            "tenant": "itest-a",
            "title": "Itest A Engineering Handbook",
            "path": "itest-a/handbook.md",
            "classification": "internal",
            "allowed_roles": [],
            "allowed_users": [],
        },
        {
            "doc_id": "itest-a-careers",
            "tenant": "itest-a",
            "title": "Itest A Careers",
            "path": "itest-a/careers.md",
            "classification": "public",
            "allowed_roles": [],
            "allowed_users": [],
        },
        {
            "doc_id": "itest-b-leave",
            "tenant": "itest-b",
            "title": "Itest B Leave Policy",
            "path": "itest-b/leave.md",
            "classification": "internal",
            "allowed_roles": [],
            "allowed_users": [],
        },
    ],
}

LONG = " ".join(
    f"Section {i}: the itest widget calibration interval is {i} hours." for i in range(1, 160)
)
HANDBOOK = " ".join(
    f"Rule {i}: deploy the zephyr pipeline only after the canary soak passes stage {i}."
    for i in range(1, 120)
)
CAREERS = " ".join(
    f"Opening {i}: apply for the marigold apprenticeship cohort number {i} today."
    for i in range(1, 30)
)
TEXTS = {
    "itest-a-leave": "Itest A staff receive 31 days of annual leave and 9 sick days per year.",
    "itest-a-salary": "Itest A salary band L4 is $123,456. " + LONG,
    "itest-a-review": "Performance review for employee_1: exceeds expectations.",
    "itest-a-handbook": HANDBOOK,
    "itest-a-careers": CAREERS,
    "itest-b-leave": "Itest B staff receive 12 days of annual leave and 4 sick days per year.",
}


def plan() -> SeedPlan:
    """Seed plan for the test world."""
    return build_plan(MANIFEST)


def seed(session: Session) -> None:
    """Upsert the test world (owner session)."""
    apply_plan(session, plan(), PASSWORD)


def docs(*ids: str) -> list[DocumentRecord]:
    """DocumentRecords (with text) for ingestion; all docs when no ids are given."""
    wanted = ids or tuple(TEXTS)
    by_id = {d["doc_id"]: d for d in MANIFEST["documents"]}
    return [
        DocumentRecord(
            doc_id=i,
            tenant=by_id[i]["tenant"],
            title=by_id[i]["title"],
            classification=by_id[i]["classification"],
            path=by_id[i]["path"],
            text=TEXTS[i],
        )
        for i in wanted
    ]


def cleanup(session: Session) -> None:
    """Delete everything the test world owns (owner session)."""
    tids = [tenant_uuid(t) for t in TENANTS]
    session.execute(delete(Chunk).where(Chunk.tenant_id.in_(tids)))
    session.execute(delete(Document).where(Document.tenant_id.in_(tids)))
    session.execute(delete(User).where(User.tenant_id.in_(tids)))
    session.execute(delete(Tenant).where(Tenant.id.in_(tids)))


def email(key: str) -> str:
    """Login email of a test user."""
    return email_for(key)


def uid(key: str):  # type: ignore[no-untyped-def]
    """User id of a test user."""
    return user_uuid(key)


def tid(slug: str):  # type: ignore[no-untyped-def]
    """Tenant id."""
    return tenant_uuid(slug)


class _Rollback(Exception):
    pass


@contextmanager
def scratch() -> Iterator[Session]:
    """Owner transaction that is ALWAYS rolled back (for deliberately corrupting data)."""
    from app.db.session import admin_transaction

    try:
        with admin_transaction() as s:
            yield s
            raise _Rollback
    except _Rollback:
        pass
