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


@dataclass
class ReconcileReport:
    drifted: list[dict] = field(default_factory=list)
    fixed: int = 0

    @property
    def clean(self) -> bool:
        return not self.drifted


def reconcile(session: Session, *, fix: bool) -> ReconcileReport:
    """Report drifted chunks; with ``fix`` repair them in the caller's (owner) transaction."""
    rep = ReconcileReport(
        drifted=[{"chunk_id": r.chunk_id, "doc_id": r.doc_id} for r in session.execute(text(FIND))]
    )
    if fix and rep.drifted:
        rep.fixed = len(session.execute(text(FIX)).all())
    return rep
