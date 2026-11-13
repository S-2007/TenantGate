"""Phase 7 mutation meta-tests: each mutant MUST make the suite's own checks fail.

M1 token_version check removed      -> a token issued before "log out everywhere" still works
M2 last-admin guard removed          -> two admins demoting each other leave the company with none
M3 invite single-use removed         -> a used invite link creates a second account
M4 dashboard tenant filter removed   -> one company's dashboard shows another company's data

Database mutants are restored from migration 0008's own SQL in ``finally``. Runs in-process
(TestClient) against the TEST branch.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from app.auth import dependencies as deps
from app.db.session import admin_transaction
from tests.integration.test_accounts import (
    active_admins,
    demote_each_other,
    invite,
    token_of,
    two_admin_company,
)
from tests.integration.test_self_service import PREFIX, _h, _login, register
from tests.integration.test_uploads import SALARY, ask_ids, company, upload
from tests.security.dynamic import purge

pytestmark = [pytest.mark.neon, pytest.mark.security]

NEW_PW = "Mutant-pass-" + "77x"


def _m8():  # type: ignore[no-untyped-def]
    path = next((Path(__file__).resolve().parents[2] / "migrations" / "versions").glob("0008_*.py"))
    spec = importlib.util.spec_from_file_location("m0008", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture()
def clean(app_role):
    purge(PREFIX)
    yield
    purge(PREFIX)


def _verdict(problems: list[str]) -> dict:
    v = {"problems": problems, "passes": not problems}
    print("MUTATION verdict:", v)
    return v


def _db_mutant(sql_mutant: str, sql_restore: str):  # type: ignore[no-untyped-def]
    class _Ctx:
        def __enter__(self):  # type: ignore[no-untyped-def]
            assert sql_mutant != sql_restore
            with admin_transaction() as s:
                s.execute(text(sql_mutant))

        def __exit__(self, *exc):  # type: ignore[no-untyped-def]
            with admin_transaction() as s:
                s.execute(text(sql_restore))

    return _Ctx()


# ------------------------------------------------------------------ M1 token version
def check_stale_session(client) -> dict:  # type: ignore[no-untyped-def]
    out = register(client).json()
    email = f"admin@{out['slug']}.example"
    old = _login(client, email)
    client.post("/auth/logout-all", headers=_login(client, email))
    r = client.get("/auth/me", headers=old)
    return _verdict([] if r.status_code == 401 else [f"expected (401,), got {r.status_code}"])


def test_intact_stale_session_rejected(clean, client):
    assert check_stale_session(client)["passes"]


def test_mutant_m1_token_version_check_removed_fails_the_suite(clean, client, monkeypatch):
    monkeypatch.setattr(deps, "session_still_valid", lambda claim, current: True)
    v = check_stale_session(client)
    assert not v["passes"], v


# ------------------------------------------------------------------ M2 last admin
def check_last_admin(client) -> dict:  # type: ignore[no-untyped-def]
    tid, a, b = two_admin_company(client)
    outcome = demote_each_other(a, b)
    n = active_admins(tid)
    return _verdict([] if n >= 1 else [f"company left with {n} active admins ({outcome})"])


def test_intact_last_admin_kept(clean, client):
    assert check_last_admin(client)["passes"]


def test_mutant_m2_last_admin_guard_removed_fails_the_suite(clean, client):
    m8 = _m8()
    with _db_mutant(m8._m6().SET_USER, m8.set_user_with_guard()):
        v = check_last_admin(client)
    assert not v["passes"], v


# ------------------------------------------------------------------ M3 invite single use
def check_invite_single_use(client) -> dict:  # type: ignore[no-untyped-def]
    out = register(client).json()
    tok = token_of(invite(client, _h(out["access_token"]), f"once@{out['slug']}.example"))
    first = client.post("/auth/accept-invite", json={"token": tok, "password": NEW_PW})
    again = client.post("/auth/accept-invite", json={"token": tok, "password": NEW_PW})
    problems = [] if first.status_code == 201 else [f"first accept got {first.status_code}"]
    problems += (
        [] if again.status_code == 410 else [f"replay: expected (410,), got {again.status_code}"]
    )
    return _verdict(problems)


def test_intact_invite_is_single_use(clean, client):
    assert check_invite_single_use(client)["passes"]


def test_mutant_m3_invite_reusable_fails_the_suite(clean, client):
    m8 = _m8()
    # the bug: forget "not used yet", and give the second account a fresh email so it inserts
    mutant = m8.ACCEPT_INVITE.replace(
        "WHERE token_hash = p_token_hash AND used_at IS NULL AND revoked_at IS NULL",
        "WHERE token_hash = p_token_hash AND revoked_at IS NULL",
    ).replace("VALUES (v_user, i.tenant_id, i.email,", "VALUES (v_user, i.tenant_id, "
              "CASE WHEN i.used_at IS NULL THEN i.email ELSE v_user::text || '@reuse.example' END,")  # fmt: skip
    with _db_mutant(mutant, m8.ACCEPT_INVITE):
        v = check_invite_single_use(client)
    assert not v["passes"], v


# ------------------------------------------------------------------ M4 dashboard scoping
def check_dashboard_scoped(client) -> dict:  # type: ignore[no-untyped-def]
    a, ha = company(client)
    b, hb = company(client)
    doc = upload(client, ha["admin"], SALARY.encode(), "salary.txt", classification="confidential",
                 allowed_roles="hr").json()["doc_id"]  # fmt: skip
    ask_ids(client, ha["hr"], SALARY)
    r = client.get("/admin/security/summary", headers=hb["admin"])
    problems = [] if r.status_code == 200 else [f"expected (200,), got {r.status_code}"]
    problems += [f"forbidden string {x!r} in response" for x in (doc, a["slug"]) if x in r.text]
    return _verdict(problems)


def test_intact_dashboard_is_tenant_scoped(clean, client):
    assert check_dashboard_scoped(client)["passes"]


def test_mutant_m4_dashboard_without_tenant_filter_fails_the_suite(clean, client):
    m8 = _m8()
    mutant = m8.SUMMARY.replace("WHERE tenant_id = v_tenant AND ts >= v_since", "WHERE ts >= v_since") \
                       .replace("JOIN public.documents d ON d.id = x.doc_id AND d.tenant_id = v_tenant",
                                "JOIN public.documents d ON d.id = x.doc_id") \
                       .replace("JOIN public.users u ON u.id = c.user_id AND u.tenant_id = v_tenant",
                                "JOIN public.users u ON u.id = c.user_id")  # fmt: skip
    with _db_mutant(mutant, m8.SUMMARY):
        v = check_dashboard_scoped(client)
    assert not v["passes"] and any("forbidden" in p for p in v["problems"]), v
