"""Append-only audit log and the admin "who retrieved doc X" endpoint, against Neon."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.api import get_rag_service
from app.config import get_settings
from app.db.session import admin_transaction, transaction
from app.generation.llm import LLMError
from tests.integration import world

pytestmark = pytest.mark.neon


def _bearer(client, key: str) -> dict[str, str]:  # type: ignore[no-untyped-def]
    r = client.post("/auth/login", json={"email": world.email(key), "password": world.PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _rows(request_id: str) -> list:  # read as the owner (BYPASSRLS), by request id
    with admin_transaction() as s:
        return s.execute(
            text("SELECT * FROM retrieval_audit WHERE request_id = :r"),
            {"r": uuid.UUID(request_id)},
        ).all()


def _insert(doc_id: str, *, tenant: str, days_ago: float, user_key: str) -> str:
    """Insert a backdated row as the owner; returns its request id.

    Audit rows are append-only, so rows from earlier test runs stay. Tests therefore assert on
    the request ids they inserted, never on total counts.
    """
    rid = str(uuid.uuid4())
    with admin_transaction() as s:
        s.execute(
            text(
                "INSERT INTO retrieval_audit (request_id, ts, event_type, user_id, tenant_id, "
                "returned_doc_ids, status, query_hash) VALUES (CAST(:rid AS uuid), "
                "now() - make_interval(secs => :secs), 'retrieval', :u, :t, ARRAY[CAST(:d AS text)], "
                "'ok', repeat('a', 64))"
            ),
            {
                "rid": rid,
                "secs": days_ago * 86400,
                "u": world.uid(user_key),
                "t": world.tid(tenant),
                "d": doc_id,
            },
        )
    return rid


# ---------------- one row per /ask ----------------
def test_success_writes_exactly_one_row(ingested, client):
    r = client.post(
        "/ask",
        json={"question": "How many sick days do Itest A staff receive?"},
        headers=_bearer(client, "itest-a:employee_1"),
    )
    assert r.status_code == 200
    rid = r.headers["x-request-id"]
    (row,) = _rows(rid)
    assert row.status == "ok" and row.http_status == 200 and row.event_type == "retrieval"
    assert row.user_id == world.uid("itest-a:employee_1") and row.tenant_id == world.tid("itest-a")
    assert row.roles == ["employee"] and len(row.query_hash) == 64
    assert row.query_text == "How many sick days do Itest A staff receive?"
    assert set(row.returned_doc_ids) == {x["doc_id"] for x in r.json()["retrieved"]}
    assert row.latency_ms > 0


def test_unauthenticated_and_invalid_requests_are_audited_as_denied(ingested, client):
    no_token = client.post("/ask", json={"question": "hi"})
    injected = client.post(
        "/ask", json={"question": "hi", "tenant_id": "x"}, headers=_bearer(client, "itest-a:hr_1")
    )
    for resp, code in ((no_token, 401), (injected, 422)):
        assert resp.status_code == code
        (row,) = _rows(resp.headers["x-request-id"])
        assert (row.status, row.http_status, row.user_id) == ("denied", code, None)


def test_llm_failure_is_audited_as_error(ingested, client):
    class Broken:
        provider, model = "fake", "broken"

        def complete(self, *a, **k):  # type: ignore[no-untyped-def]
            raise LLMError("provider down", kind="server")

    svc = get_rag_service()
    original = svc.llm
    svc.llm = Broken()
    try:
        r = client.post(
            "/ask",
            json={"question": "How many sick days do Itest A staff receive?"},
            headers=_bearer(client, "itest-a:employee_1"),
        )
    finally:
        svc.llm = original
    assert r.status_code == 502 and "provider down" not in r.text
    (row,) = _rows(r.headers["x-request-id"])
    assert (row.status, row.http_status) == ("error", 502)
    assert row.returned_chunk_ids == []  # nothing was returned to the caller


def test_store_query_text_false_keeps_only_the_hash(ingested, client, monkeypatch):
    monkeypatch.setenv("STORE_QUERY_TEXT", "false")
    get_settings.cache_clear()
    try:
        r = client.post(
            "/ask",
            json={"question": "Where is the Itest A handbook?"},
            headers=_bearer(client, "itest-a:employee_1"),
        )
    finally:
        monkeypatch.delenv("STORE_QUERY_TEXT")
        get_settings.cache_clear()
    (row,) = _rows(r.headers["x-request-id"])
    assert row.query_text is None and len(row.query_hash) == 64


def test_client_request_id_is_echoed_and_recorded(ingested, client):
    rid = str(uuid.uuid4())
    r = client.post(
        "/ask",
        json={"question": "careers"},
        headers={**_bearer(client, "itest-a:contractor_1"), "X-Request-ID": rid},
    )
    assert r.headers["x-request-id"] == rid and len(_rows(rid)) == 1


# ---------------- append-only ----------------
@pytest.mark.parametrize(
    "stmt",
    [
        "UPDATE retrieval_audit SET status = 'ok'",
        "DELETE FROM retrieval_audit",
        "TRUNCATE retrieval_audit",
    ],
)
def test_app_role_cannot_update_or_delete_audit_rows(app_role, stmt):
    with pytest.raises(DBAPIError, match="permission denied"), transaction() as s:
        s.execute(text(stmt))


@pytest.mark.parametrize(
    "stmt",
    [
        "UPDATE retrieval_audit SET status = 'ok' WHERE true",
        "DELETE FROM retrieval_audit WHERE true",
        "TRUNCATE retrieval_audit",
    ],
)
def test_even_the_owner_hits_the_append_only_trigger(ingested, stmt):
    _insert("itest-a-leave", tenant="itest-a", days_ago=0, user_key="itest-a:employee_1")
    with pytest.raises(DBAPIError, match="append-only"), admin_transaction() as s:
        s.execute(text(stmt))


def test_app_role_cannot_insert_rows_for_another_tenant(ingested):
    from app.auth.policy import AuthUser
    from app.db.rls import apply_rls_context

    hr = AuthUser(
        id=world.uid("itest-a:hr_1"), tenant_id=world.tid("itest-a"), roles=frozenset({"hr"})
    )
    with pytest.raises(DBAPIError, match="row-level security"), transaction() as s:
        apply_rls_context(s, hr)
        s.execute(
            text(
                "INSERT INTO retrieval_audit (request_id, event_type, user_id, tenant_id, status) "
                "VALUES (gen_random_uuid(), 'retrieval', :u, :t, 'ok')"
            ),
            {"u": world.uid("itest-b:employee_1"), "t": world.tid("itest-b")},
        )


# ---------------- admin endpoint ----------------
URL = "/admin/audit/documents/{}"


def test_admin_authorization_matrix(ingested, client):
    client.post(  # a real retrieval of the leave doc to find
        "/ask",
        json={"question": "How many sick days do Itest A staff receive?"},
        headers=_bearer(client, "itest-a:employee_1"),
    )
    doc = "itest-a-leave"
    assert client.get(URL.format(doc)).status_code == 401
    assert (
        client.get(URL.format(doc), headers=_bearer(client, "itest-a:employee_1")).status_code
        == 403
    )
    assert client.get(URL.format(doc), headers=_bearer(client, "itest-a:hr_1")).status_code == 403
    other = client.get(URL.format(doc), headers=_bearer(client, "itest-b:admin_1"))
    missing = client.get(URL.format("no-such-doc"), headers=_bearer(client, "itest-b:admin_1"))
    assert other.status_code == missing.status_code == 404
    assert other.json() == missing.json()  # foreign and non-existent look identical
    ok = client.get(URL.format(doc), headers=_bearer(client, "itest-a:admin_1"))
    assert ok.status_code == 200
    items = ok.json()["items"]
    assert items and {i["user_id"] for i in items} >= {str(world.uid("itest-a:employee_1"))}
    assert set(items[0]) == {"user_id", "ts", "query_hash", "request_id"}  # no query text


def test_admin_can_audit_a_confidential_doc_it_cannot_read(ingested, client):
    _insert("itest-a-salary", tenant="itest-a", days_ago=1, user_key="itest-a:hr_1")
    r = client.get(URL.format("itest-a-salary"), headers=_bearer(client, "itest-a:admin_1"))
    assert r.status_code == 200 and r.json()["items"]


def test_window_and_pagination(ingested, client):
    doc = "itest-a-careers"
    recent = {
        _insert(doc, tenant="itest-a", days_ago=d, user_key="itest-a:contractor_1")
        for d in (0.1, 0.2, 0.3, 0.4, 0.5)
    }
    old = _insert(doc, tenant="itest-a", days_ago=40, user_key="itest-a:contractor_1")  # > 30d
    foreign = _insert(doc, tenant="itest-b", days_ago=0.1, user_key="itest-b:employee_1")
    h = _bearer(client, "itest-a:admin_1")
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        body = client.get(URL.format(doc), params=params, headers=h).json()
        seen += body["items"]
        pages += 1
        cursor = body["next_cursor"]
        if not cursor:
            break
    ts = [i["ts"] for i in seen]
    assert len(seen) >= 5 and pages >= 3 and ts == sorted(ts, reverse=True)
    assert len({i["request_id"] for i in seen}) == len(seen)  # no duplicates across pages
    ids30 = {i["request_id"] for i in seen}
    ids60 = {
        i["request_id"]
        for i in client.get(URL.format(doc), params={"days": 60, "limit": 200}, headers=h).json()[
            "items"
        ]
    }
    assert recent <= ids30 and old not in ids30  # 30-day window
    assert old in ids60 and recent <= ids60  # the wider window includes the 40-day-old row
    assert foreign not in ids30 | ids60  # other tenant's rows never appear


@pytest.mark.parametrize("params", [{"days": 0}, {"days": 400}, {"limit": 0}, {"cursor": "!!"}])
def test_admin_endpoint_validates_parameters(ingested, client, params):
    r = client.get(
        URL.format("itest-a-leave"), params=params, headers=_bearer(client, "itest-a:admin_1")
    )
    assert r.status_code == 422
