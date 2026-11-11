"""Role lists frozen into migrations must match the policy module (they can't import it safely)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.auth.policy import EMPLOYEE_ROLES, KNOWN_ROLES

VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def _load(name: str):  # type: ignore[no-untyped-def]
    path = next(VERSIONS.glob(f"{name}_*.py"))
    spec = importlib.util.spec_from_file_location(f"mig_{name}", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_known_roles_in_migrations_match_policy():
    for rev in ("0002", "0003"):
        assert set(_load(rev).KNOWN_ROLES) == set(KNOWN_ROLES), rev


def test_employee_roles_in_rls_migration_match_policy():
    assert set(_load("0004").EMPLOYEE_ROLES) == set(EMPLOYEE_ROLES)


def test_migration_chain_is_linear():
    revs = {p.name[:4]: _load(p.name[:4]) for p in sorted(VERSIONS.glob("0*.py"))}
    for rev, mod in revs.items():
        expected_down = None if rev == "0001" else f"{int(rev) - 1:04d}"
        assert mod.revision == rev and mod.down_revision == expected_down
