"""Permission changes take effect on the very next query (Phase 4, sub-step A).

Revocation proofs 1-7 from the spec, the admin authorization matrix, atomicity, and an
optional revoke-latency benchmark (REVOKE_BENCH=1).
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text, update

from app.admin.acl import set_document_acl
from app.admin.reconcile import reconcile
from app.admin.verify import verify_index
from app.auth.policy import AuthUser
from app.db.models import Chunk, Document
from app.db.session import admin_transaction, transaction
from app.generation.llm import FakeLLM
from app.ingestion.embedder import HashEmbedder
from app.retrieval.service import RAGService
from tests.integration import world

pytestmark = pytest.mark.neon

REVIEW_Q = "Performance review for employee_1: exceeds expectations."
SALARY_Q = "Itest A salary band L4 widget calibration interval hours"
EMB = HashEmbedder(384)


def _login(client, key: str) -> dict[str, str]:  # type: ignore[no-untyped-def]
    r = client.post("/auth/login", json={"email": world.email(key), "password": world.PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _chunk_ids(doc_id: str) -> set[str]:
    with admin_transaction() as s:
        return {str(c) for c in s.scalars(select(Chunk.chunk_id).where(Chunk.doc_id == doc_id))}


def _retrieved(client, headers, question: str) -> set[str]:  # type: ignore[no-untyped-def]
    r = client.post("/ask", json={"question": question}, headers=headers)
    assert r.status_code == 200, r.text
    return set(r.json()["retrieved_chunk_ids"])


def _patch_acl(client, headers, doc_id, **body):  # type: ignore[no-untyped-def]
    return client.patch(f"/admin/documents/{doc_id}/acl", json=body, headers=headers)


def _admin_user() -> AuthUser:
    return AuthUser(id=world.uid("itest-a:admin_1"), tenant_id=world.tid("itest-a"),
                    roles=frozenset({"admin"}))  # fmt: skip


def _emp_user() -> AuthUser:
    return AuthUser(id=world.uid("itest-a:employee_1"), tenant_id=world.tid("itest-a"),
                    roles=frozenset({"employee"}))  # fmt: skip


# ---------------- 1-3, 5: revoke and grant, same token, next query ----------------
def test_revoke_then_grant_takes_effect_on_the_next_query(ingested, client):
    review = _chunk_ids("itest-a-review")
    emp, adm = _login(client, "itest-a:employee_1"), _login(client, "itest-a:admin_1")
    assert review & _retrieved(client, emp, REVIEW_Q)  # 1. A can retrieve D

    r = _patch_acl(client, adm, "itest-a-review", allowed_users=[], allowed_roles=["hr"])  # 2.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] and body["chunks_updated"] == len(review)
    assert body["before"]["allowed_users"] == [str(world.uid("itest-a:employee_1"))]
    assert body["after"]["allowed_roles"] == ["hr"] and body["after"]["acl_version"] == 2

    assert not review & _retrieved(client, emp, REVIEW_Q)  # 3. very next query, same token

    r = _patch_acl(
        client, adm, "itest-a-review", allowed_users=[str(world.uid("itest-a:employee_1"))]
    )
    assert r.status_code == 200  # 5. grant works immediately too
    assert review & _retrieved(client, emp, REVIEW_Q)

    with admin_transaction() as s:  # the document and every chunk moved together
        doc = s.get(Document, "itest-a-review")
        versions = set(s.scalars(select(Chunk.acl_version).where(Chunk.doc_id == doc.id)))
        audit = s.execute(
            text("SELECT target_type, change_before, change_after, user_id FROM retrieval_audit "
                 "WHERE event_type = 'acl_change' AND target_id = 'itest-a-review' "
                 "AND ts > now() - interval '5 minutes' ORDER BY ts"),
        ).all()  # fmt: skip
    assert versions == {doc.acl_version} == {3}
    assert [a.target_type for a in audit][-2:] == ["document", "document"]
    assert audit[-2].change_after["allowed_roles"] == ["hr"]
    assert audit[-1].user_id == world.uid("itest-a:admin_1")


def test_role_change_takes_effect_on_the_next_query(ingested, client):
    salary = _chunk_ids("itest-a-salary")
    hr, adm = _login(client, "itest-a:hr_1"), _login(client, "itest-a:admin_1")
    assert salary & _retrieved(client, hr, SALARY_Q)
    hr_id = world.uid("itest-a:hr_1")
    r = client.patch(f"/admin/users/{hr_id}/roles", json={"roles": ["employee"]}, headers=adm)
    assert r.status_code == 200 and r.json()["after"]["roles"] == ["employee"]
    assert not salary & _retrieved(client, hr, SALARY_Q)  # same token, roles re-read from DB
    client.patch(f"/admin/users/{hr_id}/roles", json={"roles": ["hr"]}, headers=adm)
    assert salary & _retrieved(client, hr, SALARY_Q)


# ---------------- 4: deactivation kills a still-valid token ----------------
def test_deactivated_user_token_fails_on_next_request(ingested, client):
    emp, adm = _login(client, "itest-a:employee_1"), _login(client, "itest-a:admin_1")
    assert client.post("/ask", json={"question": "hi"}, headers=emp).status_code == 200
    uid = world.uid("itest-a:employee_1")
    r = client.patch(f"/admin/users/{uid}/status", json={"is_active": False}, headers=adm)
    assert r.status_code == 200 and r.json()["after"]["is_active"] is False
    assert client.post("/ask", json={"question": "hi"}, headers=emp).status_code == 401
    assert client.get("/auth/me", headers=emp).status_code == 401
    client.patch(f"/admin/users/{uid}/status", json={"is_active": True}, headers=adm)
    assert client.get("/auth/me", headers=emp).status_code == 200


# ---------------- authorization matrix + validation + atomicity ----------------
def test_admin_write_authorization_matrix(ingested, client):
    emp, adm_a, adm_b = (
        _login(client, "itest-a:employee_1"),
        _login(client, "itest-a:admin_1"),
        _login(client, "itest-b:admin_1"),
    )
    body = {"allowed_roles": ["hr", "finance"]}
    assert _patch_acl(client, {}, "itest-a-salary", **body).status_code == 401
    assert _patch_acl(client, emp, "itest-a-salary", **body).status_code == 403
    foreign = _patch_acl(client, adm_b, "itest-a-salary", **body)
    missing = _patch_acl(client, adm_b, "no-such-doc", **body)
    assert foreign.status_code == missing.status_code == 404 and foreign.json() == missing.json()
    other_user = world.uid("itest-a:employee_1")
    r = client.patch(f"/admin/users/{other_user}/status", json={"is_active": False}, headers=adm_b)
    assert r.status_code == 404  # admin of B cannot touch a user of A
    with admin_transaction() as s:
        assert s.get(Document, "itest-a-salary").acl_version == 1  # nothing changed
    assert _patch_acl(client, adm_a, "itest-a-salary", **body).status_code == 200


@pytest.mark.parametrize(
    "body,match",
    [
        ({"classification": "internal", "allowed_roles": ["hr"]}, "cannot have"),
        ({"allowed_users": [str(uuid.uuid4())]}, "users of this tenant"),
        ({"allowed_roles": [], "allowed_users": []}, "not valid"),  # confidential, nobody
        ({"allowed_roles": ["root"]}, ""),  # unknown role: rejected by the request model
        ({}, ""),
        ({"allowed_roles": ["hr"], "tenant_id": "x"}, ""),
    ],
)
def test_invalid_acl_changes_are_rejected_and_nothing_changes(ingested, client, body, match):
    adm = _login(client, "itest-a:admin_1")
    before = _snapshot("itest-a-salary")
    r = _patch_acl(client, adm, "itest-a-salary", **body)
    assert r.status_code == 422, r.text
    assert match in r.text
    assert _snapshot("itest-a-salary") == before  # atomic: document, chunks, audit unchanged


def test_cross_tenant_user_cannot_be_granted(ingested, client):
    adm = _login(client, "itest-a:admin_1")
    r = _patch_acl(
        client, adm, "itest-a-salary", allowed_users=[str(world.uid("itest-b:employee_1"))]
    )
    assert r.status_code == 422 and "users of this tenant" in r.text


def test_admin_cannot_lock_themselves_out(ingested, client):
    adm = _login(client, "itest-a:admin_1")
    me = world.uid("itest-a:admin_1")
    assert (
        client.patch(
            f"/admin/users/{me}/status", json={"is_active": False}, headers=adm
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/admin/users/{me}/roles", json={"roles": ["employee"]}, headers=adm
        ).status_code
        == 422
    )


def test_database_function_rechecks_admin_even_if_app_layer_is_bypassed(ingested):
    """Call the DB function directly as a non-admin context: the database itself refuses."""
    from app.admin.acl import AclForbidden

    with pytest.raises(AclForbidden):
        set_document_acl(_emp_user(), "itest-a-salary", allowed_roles=["employee"],
                         request_id=str(uuid.uuid4()))  # fmt: skip
    forged = AuthUser(
        id=world.uid("itest-a:employee_1"),
        tenant_id=world.tid("itest-a"),
        roles=frozenset({"admin"}),
    )  # app.roles says admin; DB says employee
    with pytest.raises(AclForbidden):
        set_document_acl(
            forged, "itest-a-salary", allowed_roles=["employee"], request_id=str(uuid.uuid4())
        )


def _snapshot(doc_id: str) -> tuple:
    with admin_transaction() as s:
        d = s.get(Document, doc_id)
        chunks = sorted(
            (c.acl_version, tuple(c.allowed_roles), tuple(map(str, c.allowed_users)))
            for c in s.scalars(select(Chunk).where(Chunk.doc_id == doc_id))
        )
        n_audit = s.scalar(
            text("SELECT count(*) FROM retrieval_audit WHERE target_id = :d"), {"d": doc_id}
        )
        return (d.acl_version, tuple(d.allowed_roles), tuple(chunks), n_audit)


# ---------------- 6: drift is blocked by the second check, then repaired ----------------
def test_drifted_chunk_is_blocked_then_reconciled(ingested, client):
    salary = _chunk_ids("itest-a-salary")
    victim = sorted(salary)[0]
    with admin_transaction() as s:  # widen ONE chunk's ACL behind the document's back
        s.execute(
            update(Chunk).where(Chunk.chunk_id == uuid.UUID(victim))
            .values(allowed_roles=["employee", "hr"])
        )  # fmt: skip
    emp = _login(client, "itest-a:employee_1")
    got = _retrieved(client, emp, SALARY_Q)
    assert victim not in got  # layer 1+2 would admit it (chunk says employee); layer 3 drops it
    with admin_transaction() as s:
        report = reconcile(s, fix=False)
        assert [d["chunk_id"] for d in report.drifted] == [victim]
    with admin_transaction() as s:
        assert reconcile(s, fix=True).fixed == 1
    with admin_transaction() as s:
        assert reconcile(s, fix=False).clean and verify_index(s).ok


def test_reconcile_cli_check_and_fix(ingested, capsys):
    from scripts import reconcile_acls as cli

    with admin_transaction() as s:
        s.execute(
            update(Chunk).where(Chunk.doc_id == "itest-a-leave").values(acl_version=99)
        )  # fmt: skip
    assert cli.main([]) == 1  # check finds drift
    assert cli.main(["--fix"]) == 0
    assert cli.main([]) == 0
    assert '"drifted": 0' in capsys.readouterr().out


# ---------------- 7: concurrency ----------------
def test_queries_started_after_revoke_commit_never_see_the_document(ingested):
    svc = RAGService(embedder=EMB, llm=FakeLLM(), top_k=5, session_factory=transaction)
    review = _chunk_ids("itest-a-review")
    emp = _emp_user()
    log: list[tuple[float, float, bool]] = []
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            t0 = time.perf_counter()
            chunks, _, _ = svc.retrieve(REVIEW_Q, emp)
            log.append((t0, time.perf_counter(), bool(review & {str(c.chunk_id) for c in chunks})))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(3)  # queries in flight that can see D
    set_document_acl(_admin_user(), "itest-a-review", allowed_users=[], allowed_roles=["hr"],
                     request_id=str(uuid.uuid4()))  # fmt: skip
    committed_at = time.perf_counter()  # the function's transaction has committed
    time.sleep(3)
    stop.set()
    for t in threads:
        t.join()

    before = [saw for start, _, saw in log if start < committed_at]
    after = [saw for start, _, saw in log if start > committed_at]
    assert before and any(before), "sanity: D was visible before the revoke"
    assert len(after) >= 4 and not any(after), "a query started after the commit saw D"


# ---------------- revoke latency (opt-in benchmark) ----------------
@pytest.mark.skipif(not os.environ.get("REVOKE_BENCH"), reason="set REVOKE_BENCH=1 to measure")
def test_revoke_latency_benchmark(ingested):
    svc = RAGService(embedder=EMB, llm=FakeLLM(), top_k=5, session_factory=transaction)
    review, emp, adm = _chunk_ids("itest-a-review"), _emp_user(), _admin_user()
    me = [str(world.uid("itest-a:employee_1"))]

    def visible() -> bool:
        chunks, _, _ = svc.retrieve(REVIEW_Q, emp)
        return bool(review & {str(c.chunk_id) for c in chunks})

    lat, attempts, first_denied = [], [], 0
    n = int(os.environ.get("REVOKE_BENCH_N", "50"))
    for _ in range(n):
        set_document_acl(adm, "itest-a-review", allowed_users=me, request_id=str(uuid.uuid4()))
        assert visible()
        set_document_acl(adm, "itest-a-review", allowed_users=[], allowed_roles=["hr"],
                         request_id=str(uuid.uuid4()))  # fmt: skip
        t0, k = time.perf_counter(), 0
        while True:
            k += 1
            if not visible():
                break
        lat.append((time.perf_counter() - t0) * 1000)
        attempts.append(k)
        first_denied += k == 1
    q = statistics.quantiles(lat, n=20)
    out = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "revocations": n,
        "first_query_after_commit_denied": first_denied,
        "ms_patch_return_to_first_denied_query": {
            "p50": round(statistics.median(lat), 1),
            "p95": round(q[18], 1),
            "min": round(min(lat), 1),
            "max": round(max(lat), 1),
        },  # fmt: skip
        "max_attempts": max(attempts),
    }
    Path("eval/results").mkdir(parents=True, exist_ok=True)
    Path(f"eval/results/{datetime.now(UTC):%Y%m%dT%H%M%SZ}_revoke_latency.json").write_text(
        json.dumps(out, indent=2)
    )
    print(json.dumps(out, indent=2))
    assert first_denied == n
