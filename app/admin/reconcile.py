"""Find (and optionally fix) chunks whose ACL copy differs from their document's.

With atomic ACL changes (migration 0006) drift can only come from a manual edit or a bug. This
is the consistency checker for that case, and the CI integrity check. It is also what the Phase 3
second check makes safe to run late: a drifted chunk is already being dropped at query time
(fail closed) until it is repaired.

The fix copies the document's ACL onto the chunk. ``documents`` is the source of truth; a
chunk's copy is never written back to its document.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

_MISMATCH = """
    c.classification IS DISTINCT FROM d.classification
    OR c.acl_version IS DISTINCT FROM d.acl_version
    OR NOT (c.allowed_roles @> d.allowed_roles AND c.allowed_roles <@ d.allowed_roles)
    OR NOT (c.allowed_users @> d.allowed_users AND c.allowed_users <@ d.allowed_users)
"""
FIND = f"""
    SELECT c.chunk_id::text AS chunk_id, c.doc_id
    FROM chunks c JOIN documents d ON d.id = c.doc_id
    WHERE {_MISMATCH}
    ORDER BY c.doc_id, c.chunk_index
"""  # noqa: S608 - static SQL, no input
FIX = f"""
    UPDATE chunks c
       SET classification = d.classification, allowed_roles = d.allowed_roles,
           allowed_users = d.allowed_users, acl_version = d.acl_version
      FROM documents d
     WHERE d.id = c.doc_id AND ({_MISMATCH})
    RETURNING c.chunk_id::text
"""  # noqa: S608 - static SQL, no input


# Phase 6: uploaded documents go through the same checks. Two more states are repaired:
# chunks of a document that is not 'ready' (deleted: they must not exist) and chunks whose tenant
# differs from their document's (impossible with the composite FK; deleted if it ever happens).
FIND_ORPHANS = """
    SELECT c.chunk_id::text AS chunk_id, c.doc_id,
           CASE WHEN c.tenant_id IS DISTINCT FROM d.tenant_id THEN 'tenant_mismatch'
                ELSE 'document_not_ready' END AS reason
    FROM chunks c JOIN documents d ON d.id = c.doc_id
    WHERE d.status <> 'ready' OR c.tenant_id IS DISTINCT FROM d.tenant_id
    ORDER BY c.doc_id, c.chunk_index
"""
FIX_ORPHANS = """
    DELETE FROM chunks c USING documents d
     WHERE d.id = c.doc_id AND (d.status <> 'ready' OR c.tenant_id IS DISTINCT FROM d.tenant_id)
    RETURNING c.chunk_id::text
"""


@dataclass
class ReconcileReport:
    drifted: list[dict] = field(default_factory=list)
    fixed: int = 0
    orphans: list[dict] = field(default_factory=list)
    orphans_deleted: int = 0

    @property
    def clean(self) -> bool:
        return not self.drifted and not self.orphans


def reconcile(session: Session, *, fix: bool) -> ReconcileReport:
    """Report drifted chunks; with ``fix`` repair them in the caller's (owner) transaction."""
    rep = ReconcileReport(
        drifted=[{"chunk_id": r.chunk_id, "doc_id": r.doc_id} for r in session.execute(text(FIND))]
    )
    rep.orphans = [
        {"chunk_id": r.chunk_id, "doc_id": r.doc_id, "reason": r.reason}
        for r in session.execute(text(FIND_ORPHANS))
    ]
    if fix and rep.orphans:
        rep.orphans_deleted = len(session.execute(text(FIX_ORPHANS)).all())
    if fix and rep.drifted:
        rep.fixed = len(session.execute(text(FIX)).all())
    return rep
