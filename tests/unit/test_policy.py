"""Exhaustive truth table for app.auth.policy.can_access (the single source of truth).

The expected value is written out independently of the implementation (``expected`` below) and
checked against every combination of:
  classification x role set (each single role, multi-role, no role, unknown role)
  x same / other tenant x listed-in-allowed_users or not x active / inactive.
"""

from __future__ import annotations

import itertools
import uuid

import pytest

from app.auth.policy import (
    EMPLOYEE_ROLES,
    KNOWN_ROLES,
    AuthUser,
    DocACL,
    can_access,
)

T1, T2 = uuid.uuid4(), uuid.uuid4()
ME, OTHER = uuid.uuid4(), uuid.uuid4()

ROLE_SETS: list[frozenset[str]] = (
    [frozenset({r}) for r in sorted(KNOWN_ROLES)]
    + [frozenset({"manager", "finance"}), frozenset({"contractor", "hr"})]
    + [frozenset(), frozenset({"superuser"}), frozenset({"ADMIN"})]  # unknown / wrong case
)
CONFIDENTIAL_ACLS = [
    (frozenset({"admin", "finance"}), frozenset()),
    (frozenset({"admin", "hr"}), frozenset()),
    (frozenset(), frozenset({ME})),  # user-specific document
    (frozenset({"engineering"}), frozenset({OTHER})),
]


def expected(roles, same_tenant, active, classification, allowed_roles, allowed_users) -> bool:
    """Independent restatement of docs/ACCESS_MODEL.md."""
    if not active or not same_tenant:
        return False
    known = {r for r in roles if r in KNOWN_ROLES}
    if classification == "public":
        return True
    if classification == "internal":
        return bool(known & EMPLOYEE_ROLES)
    if classification == "confidential":
        return bool(known & allowed_roles) or ME in allowed_users
    return False


CASES = []
for roles, same_tenant, active in itertools.product(ROLE_SETS, (True, False), (True, False)):
    for cls in ("public", "internal"):
        CASES.append((roles, same_tenant, active, cls, frozenset(), frozenset()))
    for ar, au in CONFIDENTIAL_ACLS:
        CASES.append((roles, same_tenant, active, "confidential", ar, au))


@pytest.mark.parametrize(
    "roles,same_tenant,active,classification,allowed_roles,allowed_users",
    CASES,
    ids=[
        f"{c[3]}-{'+'.join(sorted(c[0])) or 'noroles'}-{'same' if c[1] else 'other'}-"
        f"{'active' if c[2] else 'inactive'}-{len(c[4])}r{len(c[5])}u-{i}"
        for i, c in enumerate(CASES)
    ],
)
def test_truth_table(roles, same_tenant, active, classification, allowed_roles, allowed_users):
    user = AuthUser(id=ME, tenant_id=T1, roles=roles, is_active=active)
    doc = DocACL("d", T1 if same_tenant else T2, classification, allowed_roles, allowed_users)
    assert can_access(user, doc) is expected(
        roles, same_tenant, active, classification, allowed_roles, allowed_users
    )


def test_case_count_is_exhaustive():
    # 12 role sets (7 single known roles, 2 multi-role, 3 no/unknown) x 2 tenants
    # x 2 activity states x (2 + 4) doc ACL shapes
    assert len(CASES) == len(ROLE_SETS) * 2 * 2 * 6 == 288


# --- named rules from the spec, so a failure reads as a sentence ---
def u(roles=("employee",), tenant=T1, active=True, uid=ME):
    return AuthUser(id=uid, tenant_id=tenant, roles=frozenset(roles), is_active=active)


def d(cls, roles=(), users=(), tenant=T1):
    return DocACL("doc", tenant, cls, frozenset(roles), frozenset(users))


def test_public_is_not_shared_across_tenants():
    assert can_access(u(), d("public")) and not can_access(u(tenant=T2), d("public"))


def test_contractor_reads_public_but_not_internal():
    assert can_access(u(["contractor"]), d("public"))
    assert not can_access(u(["contractor"]), d("internal"))


def test_admin_is_tenant_scoped_and_has_no_implicit_confidential_access():
    assert not can_access(u(["admin"], tenant=T2), d("internal"))
    assert not can_access(u(["admin"]), d("confidential", roles=["hr"]))
    assert can_access(u(["admin"]), d("confidential", roles=["admin", "hr"]))


def test_user_specific_document_only_for_that_user():
    doc = d("confidential", users=[ME])
    assert can_access(u(), doc)
    assert not can_access(u(uid=OTHER), doc)
    assert not can_access(u(tenant=T2), doc)  # even the same user id in another tenant


def test_inactive_user_always_denied_even_for_public():
    assert not can_access(u(["admin"], active=False), d("public"))


def test_unknown_classification_denied():
    assert not can_access(u(["admin"]), d("secret", roles=["admin"]))


def test_none_inputs_and_malformed_ids_denied():
    assert not can_access(None, d("public"))
    assert not can_access(u(), None)
    bad_user = AuthUser(id="not-a-uuid", tenant_id="also-bad", roles=frozenset({"admin"}))  # type: ignore[arg-type]
    assert not can_access(bad_user, d("public"))
    assert not can_access(u(), DocACL("x", "bad", "public"))  # type: ignore[arg-type]


def test_string_ids_are_accepted_and_garbage_users_ignored():
    doc = DocACL("x", str(T1), "confidential", frozenset(), frozenset({str(ME), "junk", 7}))  # type: ignore[arg-type]
    assert doc.allowed_users == frozenset({ME})
    assert can_access(AuthUser(id=str(ME), tenant_id=str(T1)), doc)  # type: ignore[arg-type]
    # A user whose id cannot be parsed can never match allowed_users.
    assert not can_access(AuthUser(id="junk", tenant_id=T1), doc)  # type: ignore[arg-type]


def test_is_active_must_be_exactly_true():
    assert not can_access(AuthUser(id=ME, tenant_id=T1, is_active=1), d("public"))  # type: ignore[arg-type]


def test_roles_are_immutable_and_normalised():
    user = AuthUser(id=ME, tenant_id=T1, roles=["hr", "", None, "hr"])  # type: ignore[arg-type]
    assert user.roles == frozenset({"hr"})
    with pytest.raises(AttributeError):
        user.roles = frozenset({"admin"})  # type: ignore[misc]
