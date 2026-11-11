"""Whole-table audit of the chunk index against its source of truth (``documents``).

Some checks can never fail while the database constraints are in place (missing tenant, tenant
mismatch). They are still run: the audit must not trust that a constraint was never dropped,
disabled or bypassed by a manual fix. ACL *value* equality is not a constraint, so this audit is
what catches a drifted copy.

Every check returns a count and up to ``SAMPLE`` offending ids (never chunk text).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import EMBEDDING_DIM

SAMPLE = 5

# name -> SQL returning one id column per violating row
CHECKS: dict[str, str] = {
    "chunk_missing_tenant": "SELECT c.chunk_id::text FROM chunks c WHERE c.tenant_id IS NULL",
    "chunk_unknown_tenant": """
        SELECT c.chunk_id::text FROM chunks c
        LEFT JOIN tenants t ON t.id = c.tenant_id WHERE t.id IS NULL""",
    "chunk_without_document": """
        SELECT c.chunk_id::text FROM chunks c
        LEFT JOIN documents d ON d.id = c.doc_id WHERE d.id IS NULL""",
    "chunk_tenant_mismatch": """
        SELECT c.chunk_id::text FROM chunks c
        JOIN documents d ON d.id = c.doc_id WHERE c.tenant_id IS DISTINCT FROM d.tenant_id""",
    "chunk_acl_mismatch": """
        SELECT c.chunk_id::text FROM chunks c
        JOIN documents d ON d.id = c.doc_id
        WHERE c.classification IS DISTINCT FROM d.classification
           OR c.acl_version IS DISTINCT FROM d.acl_version
           OR NOT (c.allowed_roles @> d.allowed_roles AND c.allowed_roles <@ d.allowed_roles)
           OR NOT (c.allowed_users @> d.allowed_users AND c.allowed_users <@ d.allowed_users)""",
    "chunk_bad_acl_shape": """
        SELECT c.chunk_id::text FROM chunks c
        WHERE (c.classification = 'confidential'
               AND cardinality(c.allowed_roles) = 0 AND cardinality(c.allowed_users) = 0)
           OR (c.classification IN ('public', 'internal')
               AND (cardinality(c.allowed_roles) > 0 OR cardinality(c.allowed_users) > 0))""",
    "chunk_bad_embedding_dim": """
        SELECT c.chunk_id::text FROM chunks c
        WHERE c.embedding IS NULL OR vector_dims(c.embedding) <> :dim""",
    "document_user_from_other_tenant": """
        SELECT DISTINCT d.id FROM documents d
        CROSS JOIN LATERAL unnest(d.allowed_users) AS au(user_id)
        LEFT JOIN users u ON u.id = au.user_id
        WHERE u.id IS NULL OR u.tenant_id <> d.tenant_id""",
    "user_unknown_tenant": """
        SELECT u.id::text FROM users u
        LEFT JOIN tenants t ON t.id = u.tenant_id WHERE t.id IS NULL""",
}

# Reported, but not violations (a seeded document that has not been ingested yet is fine).
INFO: dict[str, str] = {
    "documents_without_chunks": """
        SELECT d.id FROM documents d
        WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.doc_id = d.id)""",
}


@dataclass
class CheckResult:
    name: str
    count: int
    sample: list[str] = field(default_factory=list)


@dataclass
class VerifyReport:
    total_chunks: int
    total_documents: int
    violations: list[CheckResult]
    info: list[CheckResult]

    @property
    def ok(self) -> bool:
        """True when no check found a violating row."""
        return all(v.count == 0 for v in self.violations)


def _run(session: Session, name: str, sql: str) -> CheckResult:
    params = {"dim": EMBEDDING_DIM} if ":dim" in sql else {}
    ids = [str(r[0]) for r in session.execute(text(sql), params)]
    return CheckResult(name, len(ids), sorted(ids)[:SAMPLE])


def verify_index(session: Session) -> VerifyReport:
    """Run every check in the caller's transaction (sees its uncommitted rows too)."""
    return VerifyReport(
        total_chunks=int(session.execute(text("SELECT count(*) FROM chunks")).scalar_one()),
        total_documents=int(session.execute(text("SELECT count(*) FROM documents")).scalar_one()),
        violations=[_run(session, n, q) for n, q in CHECKS.items()],
        info=[_run(session, n, q) for n, q in INFO.items()],
    )
