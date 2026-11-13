"""Phase 7 against Neon: invites, session revocation, last-admin race, security dashboard."""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text

from app.auth.policy import AuthUser
from app.db.rls import apply_rls_context
from app.db.session import admin_transaction, transaction
from tests.integration import world
from tests.integration.test_self_service import PW, _h, _login, register
from tests.integration.test_uploads import LEAVE, SALARY, ask_ids, company, upload

pytestmark = pytest.mark.neon

NEW_PW = "Brand-new-pass-" + "42x"  # test-only


def invite(client, h, email: str, roles=("employee",)):  # type: ignore[no-untyped-def]
    return client.post("/admin/invites", headers=h, json={"email": email, "roles": list(roles)})


def token_of(r) -> str:  # type: ignore[no-untyped-def]
    url = r.json()["invite_url"]
    assert "#invite=" in url and "?" not in url  # fragment: never sent to a server
    return url.split("#invite=", 1)[1]


# ------------------------------------------------------------------ invites
def test_invite_accept_once_and_uniform_410(fresh, client):
    out = register(client).json()
    h, slug = _h(out["access_token"]), out["slug"]
    r = invite(client, h, f"new@{slug}.example", ["hr", "employee"])
    assert r.status_code == 201, r.text
    tok = token_of(r)
    with admin_transaction() as s:  # only the hash is stored
        stored = s.execute(text("SELECT token_hash FROM invites WHERE email = :e"),
                           {"e": f"new@{slug}.example"}).scalar_one()  # fmt: skip
    assert tok not in stored and len(stored) == 64
    pv = client.post("/auth/invite-preview", json={"token": tok})
    assert pv.status_code == 200 and pv.json()["email"] == f"new@{slug}.example"
    acc = client.post("/auth/accept-invite", json={"token": tok, "password": NEW_PW})
    assert acc.status_code == 201, acc.text
    me = client.get("/auth/me", headers=_h(acc.json()["access_token"])).json()
    assert me["tenant_id"] == out["tenant_id"] and me["roles"] == ["employee", "hr"]
    assert (
        client.post(
            "/auth/login", json={"email": f"new@{slug}.example", "password": NEW_PW}
        ).status_code
        == 200
    )
    # replay, preview after use, a random token, a revoked and an expired invite: same 410
    r2 = invite(client, h, f"rev@{slug}.example")
    rev_tok = token_of(r2)
    assert client.delete(f"/admin/invites/{r2.json()['invite_id']}", headers=h).status_code == 200
    r3 = invite(client, h, f"exp@{slug}.example")
    exp_tok = token_of(r3)
    with admin_transaction() as s:
        s.execute(text("UPDATE invites SET expires_at = now() - interval '1 minute' WHERE id = :i"),
                  {"i": r3.json()["invite_id"]})  # fmt: skip
    bodies = []
    for t in (tok, rev_tok, exp_tok, "A" * 43):
        for path, body in (("/auth/accept-invite", {"token": t, "password": NEW_PW}),
                           ("/auth/invite-preview", {"token": t})):  # fmt: skip
            res = client.post(path, json=body)
            assert res.status_code == 410, (path, t[:6], res.text)
            bodies.append(res.json())
    assert all(b == bodies[0] for b in bodies)
    st = {i["email"]: i["status"] for i in client.get("/admin/invites", headers=h).json()}
    assert st == {f"new@{slug}.example": "accepted", f"rev@{slug}.example": "revoked",
                  f"exp@{slug}.example": "expired"}  # fmt: skip
    with admin_transaction() as s:
        ev = s.execute(text("SELECT event_type FROM retrieval_audit WHERE tenant_id = :t "
                            "AND event_type LIKE 'invite%' ORDER BY ts"), {"t": out["tenant_id"]}).scalars().all()  # fmt: skip
    assert (
        ev.count("invite_created") == 3
        and ev.count("invite_accepted") == 1
        and "invite_revoked" in ev
    )


def test_invite_denials(fresh, client):
    a = register(client).json()
    b = register(client).json()
    ha, hb, slug = _h(a["access_token"]), _h(b["access_token"]), a["slug"]
    assert invite(client, {}, f"x@{slug}.example").status_code == 401
    emp = _login(client, world.email("itest-a:employee_1"), world.PASSWORD)
    assert invite(client, emp, f"x@{slug}.example").status_code == 403
    for bad in ({"email": f"x@{slug}.example", "roles": ["contractor"]},
                {"email": f"x@{slug}.example", "roles": ["employee"], "tenant_id": b["tenant_id"]},
                {"email": "not-an-email", "roles": ["employee"]}):  # fmt: skip
        assert client.post("/admin/invites", headers=ha, json=bad).status_code == 422
    assert invite(client, ha, world.email("itest-a:hr_1")).status_code == 409  # existing user
    r = invite(client, ha, f"y@{slug}.example")
    # company B can neither see nor revoke company A's invite
    assert client.delete(f"/admin/invites/{r.json()['invite_id']}", headers=hb).status_code == 404
    assert all(
        i["email"] != f"y@{slug}.example" for i in client.get("/admin/invites", headers=hb).json()
    )
    # the accepted user lands in the INVITING company, whatever the client sends
    acc = client.post("/auth/accept-invite", json={"token": token_of(r), "password": NEW_PW,
                                                   "tenant_id": b["tenant_id"]})  # fmt: skip
    assert acc.status_code == 422
    acc = client.post("/auth/accept-invite", json={"token": token_of(r), "password": NEW_PW})
    assert (
        client.get("/auth/me", headers=_h(acc.json()["access_token"])).json()["tenant_id"]
        == a["tenant_id"]
    )
    assert (
        client.post("/auth/accept-invite", json={"token": "short", "password": NEW_PW}).status_code
        == 422
    )


# ------------------------------------------------------------------ sessions
def test_logout_all_and_change_password_revoke_old_tokens(fresh, client):
    out = register(client).json()
    slug = out["slug"]
    email = f"admin@{slug}.example"
    t1, t2 = _login(client, email), _login(client, email)  # two "browsers"
    assert client.post("/auth/logout-all", headers=t1).status_code == 200
    assert client.get("/auth/me", headers=t1).status_code == 401
    assert client.get("/auth/me", headers=t2).status_code == 401
    t3 = _login(client, email)
    assert client.get("/auth/me", headers=t3).status_code == 200
    wrong = client.post("/auth/change-password", headers=t3,
                        json={"current_password": "wrong-password-1", "new_password": NEW_PW})  # fmt: skip
    assert wrong.status_code == 403
    ok = client.post("/auth/change-password", headers=t3,
                     json={"current_password": PW, "new_password": NEW_PW})  # fmt: skip
    assert ok.status_code == 200, ok.text
    assert client.get("/auth/me", headers=t3).status_code == 401  # the old session died
    assert client.get("/auth/me", headers=_h(ok.json()["access_token"])).status_code == 200
    assert client.post("/auth/login", json={"email": email, "password": PW}).status_code == 401
    assert client.post("/auth/login", json={"email": email, "password": NEW_PW}).status_code == 200
    with admin_transaction() as s:
        ev = s.execute(text("SELECT event_type FROM retrieval_audit WHERE tenant_id = :t AND "
                            "event_type IN ('sessions_revoked','password_changed') ORDER BY ts"),
                       {"t": out["tenant_id"]}).scalars().all()  # fmt: skip
    assert ev == ["sessions_revoked", "password_changed"]


def test_admin_revokes_another_users_sessions(fresh, client):
    out, hs = company(client)
    other, ho = company(client)
    emp_id = next(u["id"] for u in client.get("/admin/users", headers=hs["admin"]).json()
                  if u["email"].startswith("employee@"))  # fmt: skip
    assert client.get("/auth/me", headers=hs["employee"]).status_code == 200
    assert (
        client.post(f"/admin/users/{emp_id}/revoke-sessions", headers=ho["admin"]).status_code
        == 404
    )
    assert (
        client.post(f"/admin/users/{emp_id}/revoke-sessions", headers=hs["hr"]).status_code == 403
    )
    assert client.get("/auth/me", headers=hs["employee"]).status_code == 200
    assert (
        client.post(f"/admin/users/{emp_id}/revoke-sessions", headers=hs["admin"]).status_code
        == 200
    )
    assert client.get("/auth/me", headers=hs["employee"]).status_code == 401


# ------------------------------------------------------------------ last admin (race)
def two_admin_company(client):  # type: ignore[no-untyped-def]
    out = register(client).json()
    h = _h(out["access_token"])
    r = client.post("/admin/users", headers=h, json={"email": f"admin2@{out['slug']}.example",
                    "roles": ["admin"], "password": PW})  # fmt: skip
    tid = uuid.UUID(out["tenant_id"])
    a = AuthUser(id=uuid.UUID(out["user_id"]), tenant_id=tid, roles=frozenset({"admin"}))
    b = AuthUser(id=uuid.UUID(r.json()["id"]), tenant_id=tid, roles=frozenset({"admin"}))
    return tid, a, b


def demote_each_other(a: AuthUser, b: AuthUser) -> list[str]:
    """A demotes B in an open transaction; meanwhile B demotes A; then A commits.

    Deterministic interleaving of the race two admins can hit in the UI. Returns each outcome.
    """
    results: dict[str, str] = {}
    b_started = threading.Event()

    def b_side() -> None:
        try:
            with transaction() as s:
                apply_rls_context(s, b)
                b_started.set()
                s.execute(text("SELECT admin_set_user(CAST(:u AS uuid), ARRAY['employee'], NULL, "
                               "gen_random_uuid())"), {"u": str(a.id)})  # fmt: skip
            results["b"] = "ok"
        except Exception as exc:  # noqa: BLE001
            results["b"] = getattr(getattr(exc, "orig", None), "sqlstate", type(exc).__name__)

    with transaction() as s:
        apply_rls_context(s, a)
        s.execute(text("SELECT admin_set_user(CAST(:u AS uuid), ARRAY['employee'], NULL, "
                       "gen_random_uuid())"), {"u": str(b.id)})  # fmt: skip
        t = threading.Thread(target=b_side)
        t.start()
        b_started.wait(30)
        t.join(3)  # B is now blocked (lock) or already done (no guard)
        results["a"] = "ok"
    t.join(60)
    return [results.get("a", "?"), results.get("b", "?")]


def active_admins(tid: uuid.UUID) -> int:
    with admin_transaction() as s:
        return s.execute(text("SELECT count(*) FROM users WHERE tenant_id = :t AND is_active "
                              "AND 'admin' = ANY(roles)"), {"t": tid}).scalar_one()  # fmt: skip


def test_two_admins_demoting_each_other_keep_one_admin(fresh, client):
    tid, a, b = two_admin_company(client)
    outcome = demote_each_other(a, b)
    assert outcome[0] == "ok" and outcome[1] in ("TG409", "42501"), outcome
    assert active_admins(tid) == 1


def test_self_demotion_still_refused(fresh, client):
    out = register(client).json()
    r = client.patch(f"/admin/users/{out['user_id']}/roles", headers=_h(out["access_token"]),
                     json={"roles": ["employee"]})  # fmt: skip
    assert r.status_code == 422


# ------------------------------------------------------------------ security dashboard
def test_security_summary_is_tenant_scoped(fresh, client):
    out, hs = company(client)
    other, ho = company(client)
    p = upload(client, hs["admin"], LEAVE.encode(), "leave.txt").json()["doc_id"]
    c = upload(client, hs["admin"], SALARY.encode(), "salary.txt", classification="confidential",
               allowed_roles="hr").json()["doc_id"]  # fmt: skip
    for _ in range(2):
        assert c in ask_ids(client, hs["hr"], SALARY)
    ask_ids(client, hs["employee"], LEAVE)
    invite(client, hs["admin"], f"later@{out['slug']}.example")
    r = client.get("/admin/security/summary?days=7", headers=hs["admin"])
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["asks"] == 3 and s["distinct_users"] == 2
    conf = {d["doc_id"]: d for d in s["confidential_documents"]}
    assert conf[c]["reads"] == 2 and conf[c]["readers"] == 1
    assert conf[c]["title"] is None  # the admin isn't HR: id only, never the title
    assert p not in conf  # internal docs aren't listed as confidential
    assert [x["email"] for x in s["confidential_readers"]] == [f"hr@{out['slug']}.example"]
    assert s["uploads_by_day"][0]["uploaded"] == 2
    assert any(e["event"] == "invite_created" for e in s["admin_events"])
    # another company's dashboard has none of it
    other_s = client.get("/admin/security/summary", headers=ho["admin"])
    assert other_s.status_code == 200 and other_s.json()["asks"] == 0
    assert c not in other_s.text and out["slug"] not in other_s.text
    assert client.get("/admin/security/summary", headers=hs["hr"]).status_code == 403
    assert client.get("/admin/security/summary?days=0", headers=hs["admin"]).status_code == 422
