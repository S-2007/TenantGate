"""Phase 6 sub-step B against Neon: upload -> background ingestion -> role-restricted answers."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db.session import admin_transaction
from tests import upload_files as uf
from tests.integration import world
from tests.integration.test_self_service import PW, _h, _login, register

pytestmark = pytest.mark.neon

LEAVE = ("Zephyr Dynamics leave policy: every Zephyr employee receives 31 days of paid leave "
         "per year, plus 4 wellness days. ZEPHYR-MARKER-LEAVE.")  # fmt: skip
SALARY = ("Zephyr Dynamics salary bands: L4 engineers earn 151,000 to 177,000 per year. "
          "ZEPHYR-MARKER-SALARY confidential HR data.")  # fmt: skip


def company(client):  # type: ignore[no-untyped-def]
    """A fresh company with admin, hr and employee users. Returns (slug, headers by role)."""
    out = register(client).json()
    slug, ha = out["slug"], _h(out["access_token"])
    hs = {"admin": ha}
    for role in ("hr", "employee"):
        r = client.post("/admin/users", headers=ha, json={"email": f"{role}@{slug}.example",
                        "roles": [role] if role == "employee" else ["hr", "employee"],
                        "password": PW})  # fmt: skip
        assert r.status_code == 201, r.text
        hs[role] = _login(client, f"{role}@{slug}.example")
    return out, hs


def upload(client, h, body: bytes, name: str = "doc.txt", **fields):  # type: ignore[no-untyped-def]
    data = {"classification": "internal", "title": name, **fields}
    return client.post("/documents/upload", headers=h, files={"file": (name, body, "text/plain")},
                       data=data)  # fmt: skip


def ask_ids(client, h, q: str) -> set[str]:  # type: ignore[no-untyped-def]
    r = client.post("/ask", headers=h, json={"question": q})
    assert r.status_code == 200, r.text
    return {x["doc_id"] for x in r.json()["retrieved"]}


def test_upload_ingest_and_role_restricted_answers(fresh, client):
    out, hs = company(client)
    pub = upload(client, hs["admin"], LEAVE.encode(), "leave.txt")
    conf = upload(client, hs["admin"], uf.docx([SALARY]), "salary.docx",
                  classification="confidential", allowed_roles="hr")  # fmt: skip
    assert pub.status_code == conf.status_code == 202, (pub.text, conf.text)
    p, c = pub.json()["doc_id"], conf.json()["doc_id"]
    assert p.startswith("up-") and pub.json()["status"] == "processing"
    for d in (p, c):  # TestClient runs background tasks before returning
        st = client.get(f"/documents/{d}/status", headers=hs["admin"]).json()
        assert st["status"] == "ready" and st["chunk_count"] >= 1, st
    # employee: sees and retrieves the internal doc only
    emp_docs = {d["doc_id"] for d in client.get("/documents", headers=hs["employee"]).json()}
    assert p in emp_docs and c not in emp_docs
    assert c not in ask_ids(client, hs["employee"], "What are the Zephyr L4 salary bands?")
    assert client.get(f"/documents/{c}", headers=hs["employee"]).status_code == 404
    # hr: retrieves the confidential one
    assert c in ask_ids(client, hs["hr"], "What are the Zephyr L4 salary bands?")
    assert client.get(f"/documents/{c}", headers=hs["hr"]).json()["status"] == "ready"
    # another company never sees either
    seed = _login(client, world.email("itest-a:hr_1"), world.PASSWORD)
    assert not ({p, c} & ask_ids(client, seed, "Zephyr leave policy and salary bands"))
    assert client.get(f"/documents/{p}", headers=seed).status_code == 404
    # chunks carry the document's ACL, copied by the database
    with admin_transaction() as s:
        rows = s.execute(text("SELECT DISTINCT tenant_id, classification::text, allowed_roles "
                              "FROM chunks WHERE doc_id = :d"), {"d": c}).all()  # fmt: skip
        ev = s.execute(text("SELECT event_type FROM retrieval_audit WHERE target_id IN (:p, :c) "
                            "ORDER BY ts"), {"p": p, "c": c}).scalars().all()  # fmt: skip
    assert [tuple(r) for r in rows] == [(uuid.UUID(out["tenant_id"]), "confidential", ["hr"])]
    assert ev == ["document_uploaded", "document_uploaded"]


def test_processing_document_is_invisible_in_every_layer(fresh, client):
    """Chunks of a non-ready document are hidden by RLS, the WHERE clause and the post-check."""
    from app.auth.policy import AuthUser
    from app.uploads import service

    out, hs = company(client)
    admin = AuthUser(id=uuid.UUID(out["user_id"]), tenant_id=uuid.UUID(out["tenant_id"]),
                     roles=frozenset({"admin"}))  # fmt: skip
    b = service.begin(admin, service.UploadMeta("Stuck", "public", [], []), size=10,
                      content_type="text/plain", filename="s.txt", request_id=str(uuid.uuid4()))  # fmt: skip
    doc = b["doc_id"]
    good = upload(client, hs["admin"], LEAVE.encode(), "leave.txt").json()["doc_id"]
    # simulate a half-written upload: copy the ready doc's chunks under the processing doc
    with admin_transaction() as s:
        s.execute(text(
            "INSERT INTO chunks (chunk_id, doc_id, tenant_id, title, chunk_index, text, embedding,"
            " classification, allowed_roles, allowed_users, acl_version) "
            "SELECT gen_random_uuid(), :doc, tenant_id, title, chunk_index, text, embedding, "
            "'public', '{}', '{}', 1 FROM chunks WHERE doc_id = :good"), {"doc": doc, "good": good})  # fmt: skip
    got = ask_ids(client, hs["employee"], LEAVE)
    assert good in got and doc not in got
    assert doc not in {d["doc_id"] for d in client.get("/documents", headers=hs["admin"]).json()}
    assert client.get("/documents/mine", headers=hs["admin"]).json()[0]["status"] in (
        "processing",
        "ready",
    )
    # verify_index flags the state; reconcile --fix deletes exactly those chunks
    from app.admin.reconcile import reconcile
    from app.admin.verify import verify_index

    with admin_transaction() as s:
        v = {r.name: r.count for r in verify_index(s).violations}
        assert v["chunk_of_non_ready_document"] >= 1
        rep = reconcile(s, fix=True)
        assert {o["doc_id"] for o in rep.orphans} == {doc} and rep.orphans_deleted >= 1
    with admin_transaction() as s:
        assert {r.name: r.count for r in verify_index(s).violations}[
            "chunk_of_non_ready_document"
        ] == 0


def test_upload_permissions_and_cross_tenant(fresh, client, monkeypatch):
    out, hs = company(client)
    other, ho = company(client)
    assert upload(client, hs["employee"], LEAVE.encode()).status_code == 403
    assert upload(client, {}, LEAVE.encode()).status_code == 401
    monkeypatch.setattr(get_settings(), "upload_roles", "admin,hr")
    assert upload(client, hs["hr"], LEAVE.encode(), "hr.txt").status_code == 202
    d = upload(client, hs["admin"], LEAVE.encode()).json()["doc_id"]
    for h in (ho["admin"], ho["hr"]):  # other company: everything is 404
        assert client.get(f"/documents/{d}", headers=h).status_code == 404
        assert client.get(f"/documents/{d}/status", headers=h).status_code == 404
        assert client.delete(f"/documents/{d}", headers=h).status_code == 404
        r = client.put(f"/documents/{d}/content", headers=h,
                       files={"file": ("x.txt", SALARY.encode(), "text/plain")})  # fmt: skip
        assert r.status_code == 404
    # ACL naming a user of another company is rejected
    other_user = client.get("/admin/users", headers=ho["admin"]).json()[0]["id"]
    r = upload(client, hs["admin"], SALARY.encode(), classification="confidential",
               allowed_users=other_user)  # fmt: skip
    assert r.status_code == 422 and "your company" in r.text
    # client-chosen identity / ids in the form are refused
    for extra in ({"tenant_id": other["tenant_id"]}, {"doc_id": "up-mine"}, {"uploaded_by": "x"}):
        assert upload(client, hs["admin"], LEAVE.encode(), **extra).status_code == 422
    # bad ACL shapes
    assert (
        upload(client, hs["admin"], LEAVE.encode(), classification="confidential").status_code
        == 422
    )
    assert upload(client, hs["admin"], LEAVE.encode(), allowed_roles="hr").status_code == 422
    assert upload(client, hs["admin"], LEAVE.encode(), classification="secret").status_code == 422


def test_bad_files_and_limits(fresh, client, monkeypatch):
    _, hs = company(client)
    h = hs["admin"]
    assert upload(client, h, uf.EXE, "payroll.pdf").status_code == 415
    assert upload(client, h, b"hello", "page.html").status_code == 415
    assert upload(client, h, b"", "empty.txt").status_code == 422
    r = upload(client, h, b"%PDF-1.4\n not really a pdf at all", "broken.pdf")
    assert r.status_code == 202
    st = client.get(f"/documents/{r.json()['doc_id']}/status", headers=h).json()
    assert st["status"] == "failed" and "PDF" in st["last_error"]
    r = upload(client, h, uf.zip_bomb_docx(40), "bomb.docx")
    assert r.status_code in (202, 413)
    if r.status_code == 202:
        assert (
            client.get(f"/documents/{r.json()['doc_id']}/status", headers=h).json()["status"]
            == "failed"
        )
    monkeypatch.setattr(get_settings(), "max_upload_mb", 0.01)
    assert upload(client, h, b"x" * 20_000, "big.txt").status_code == 413
    monkeypatch.setattr(get_settings(), "max_upload_mb", 5.0)
    two = client.post("/documents/upload", headers=h, data={"classification": "public"},
                      files=[("file", ("a.txt", LEAVE.encode(), "text/plain")),
                             ("file", ("b.txt", LEAVE.encode(), "text/plain"))])  # fmt: skip
    assert two.status_code == 422
    with admin_transaction() as s:
        n = s.execute(text("SELECT count(*) FROM documents d JOIN tenants t ON t.id = d.tenant_id "
                           "WHERE t.slug LIKE 'itest-ss-%'")).scalar_one()  # fmt: skip
    monkeypatch.setattr(get_settings(), "max_documents_per_tenant", n)
    r = upload(client, h, LEAVE.encode())
    assert r.status_code == 409 and "quota" in r.json()["detail"]


def test_replace_and_delete(fresh, client):
    _, hs = company(client)
    h = hs["admin"]
    d = upload(client, h, LEAVE.encode(), "leave.txt", classification="public").json()["doc_id"]
    v1 = client.get(f"/documents/{d}", headers=h).json()["acl_version"]
    r = client.put(f"/documents/{d}/content", headers=h,
                   files={"file": ("leave-v2.txt", (LEAVE + " UPDATED-V2 now 33 days.").encode(),
                                   "text/plain")})  # fmt: skip
    assert r.status_code == 202, r.text
    m = client.get(f"/documents/{d}", headers=h).json()
    assert m["status"] == "ready" and m["acl_version"] == v1 + 1
    assert m["original_filename"] == "leave-v2.txt"
    with admin_transaction() as s:
        texts = s.execute(text("SELECT text, acl_version FROM chunks WHERE doc_id = :d"),
                          {"d": d}).all()  # fmt: skip
    assert any("UPDATED-V2" in t for t, _ in texts) and {v for _, v in texts} == {v1 + 1}
    # replacing with a bad file keeps the previous version searchable
    r = client.put(f"/documents/{d}/content", headers=h,
                   files={"file": ("x.pdf", b"%PDF-1.4 broken", "application/pdf")})  # fmt: skip
    assert r.status_code == 202
    m = client.get(f"/documents/{d}", headers=h).json()
    assert m["status"] == "ready" and m["last_error"] and m["acl_version"] == v1 + 1
    assert d in ask_ids(client, hs["employee"], LEAVE)
    # the employee may not delete; the admin may
    assert client.delete(f"/documents/{d}", headers=hs["employee"]).status_code == 404
    r = client.delete(f"/documents/{d}", headers=h)
    assert r.status_code == 200 and r.json()["chunks_deleted"] >= 1
    assert client.get(f"/documents/{d}", headers=h).status_code == 404
    assert d not in ask_ids(client, hs["employee"], LEAVE)
    with admin_transaction() as s:
        ev = s.execute(text("SELECT event_type, status FROM retrieval_audit WHERE target_id = :d "
                            "ORDER BY ts"), {"d": d}).all()  # fmt: skip
        left = s.execute(
            text("SELECT count(*) FROM chunks WHERE doc_id = :d"), {"d": d}
        ).scalar_one()
    assert [tuple(e) for e in ev] == [("document_uploaded", "ok"), ("document_replaced", "ok"),
                                      ("document_replaced", "error"), ("document_deleted", "ok")]  # fmt: skip
    assert left == 0
