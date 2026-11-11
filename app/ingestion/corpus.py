"""Corpus + manifest loading and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.auth.policy import CLASSIFICATIONS, KNOWN_ROLES

ROLES = tuple(sorted(KNOWN_ROLES))


class ManifestError(ValueError):
    """The manifest is inconsistent with itself or with the files on disk."""


@dataclass(frozen=True)
class DocumentRecord:
    """One source document as described by the manifest.

    The manifest ACL fields are the *seed input* for the ``documents`` table. From Phase 2 on,
    ingestion reads the ACL from Postgres, never from here.
    """

    doc_id: str
    tenant: str
    title: str
    classification: str
    path: str
    allowed_roles: tuple[str, ...] = ()
    allowed_users: tuple[str, ...] = ()
    text: str = field(default="", repr=False)


def load_manifest(corpus_path: str | Path) -> dict:
    """Read ``manifest.json`` from the corpus folder."""
    p = Path(corpus_path) / "manifest.json"
    if not p.exists():
        raise ManifestError(f"{p} not found")
    return json.loads(p.read_text(encoding="utf-8"))


def validate_manifest(manifest: dict, corpus_path: str | Path) -> list[str]:
    """Return a list of problems (empty list == valid)."""
    root = Path(corpus_path)
    problems: list[str] = []
    tenants = {t["tenant_id"] for t in manifest.get("tenants", [])}
    user_keys = {u["key"] for u in manifest.get("users", [])}
    for u in manifest.get("users", []):
        roles = u.get("roles")
        if not isinstance(roles, list) or not roles:
            problems.append(f"user {u.get('key')}: roles must be a non-empty list")
        elif set(roles) - set(ROLES):
            problems.append(f"user {u.get('key')}: unknown roles {sorted(set(roles) - set(ROLES))}")
        if u.get("tenant") not in tenants:
            problems.append(f"user {u.get('key')}: unknown tenant {u.get('tenant')!r}")
        if not str(u.get("key", "")).startswith(f"{u.get('tenant')}:"):
            problems.append(f"user {u.get('key')}: key must start with '<tenant>:'")
        if not isinstance(u.get("active", True), bool):
            problems.append(f"user {u.get('key')}: active must be true/false")
    seen: set[str] = set()
    for d in manifest.get("documents", []):
        did = d.get("doc_id", "?")
        if did in seen:
            problems.append(f"duplicate doc_id {did}")
        seen.add(did)
        if d.get("tenant") not in tenants:
            problems.append(f"{did}: unknown tenant {d.get('tenant')!r}")
        if not did.startswith(f"{d.get('tenant')}-"):
            problems.append(f"{did}: doc_id must start with '<tenant>-'")
        if d.get("classification") not in CLASSIFICATIONS:
            problems.append(f"{did}: bad classification {d.get('classification')!r}")
        bad_roles = set(d.get("allowed_roles", [])) - set(ROLES)
        if bad_roles:
            problems.append(f"{did}: unknown roles {sorted(bad_roles)}")
        for uk in d.get("allowed_users", []):
            if uk not in user_keys:
                problems.append(f"{did}: unknown user key {uk}")
            elif not uk.startswith(f"{d.get('tenant')}:"):
                problems.append(f"{did}: user {uk} belongs to another tenant")
        has_acl = bool(d.get("allowed_roles") or d.get("allowed_users"))
        if d.get("classification") == "confidential" and not has_acl:
            problems.append(f"{did}: confidential needs allowed_roles or allowed_users")
        if d.get("classification") in ("public", "internal") and has_acl:
            # Semantics come from the classification alone (docs/ACCESS_MODEL.md); a role list
            # here would look like a restriction that is not enforced.
            problems.append(
                f"{did}: {d.get('classification')} documents must have empty allowed_roles/users"
            )
        f = root / d.get("path", "")
        if not f.is_file():
            problems.append(f"{did}: file {d.get('path')} missing")
        elif not f.read_text(encoding="utf-8").strip():
            problems.append(f"{did}: file is empty")
    return problems


def load_documents(corpus_path: str | Path, *, tenant: str | None = None) -> list[DocumentRecord]:
    """Load every manifest document (optionally one tenant) with its text, sorted by doc_id."""
    root = Path(corpus_path)
    manifest = load_manifest(root)
    problems = validate_manifest(manifest, root)
    if problems:
        raise ManifestError("; ".join(problems[:10]))
    docs = []
    for d in manifest["documents"]:
        if tenant and d["tenant"] != tenant:
            continue
        docs.append(
            DocumentRecord(
                doc_id=d["doc_id"],
                tenant=d["tenant"],
                title=d["title"],
                classification=d["classification"],
                path=d["path"],
                allowed_roles=tuple(d.get("allowed_roles", [])),
                allowed_users=tuple(d.get("allowed_users", [])),
                text=(root / d["path"]).read_text(encoding="utf-8"),
            )
        )
    return sorted(docs, key=lambda x: x.doc_id)
