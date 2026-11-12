"""Phase 6 mutation meta-tests for the upload paths: each mutant MUST make the checks fail.

(i)   the upload handler trusts a tenant_id sent in the form
(ii)  the database skips the same-tenant check on allowed_users
(iii) the status = 'ready' filter is disabled (in every layer) -> an unfinished document is
      retrievable

The checks are the suite's own: expected status codes, ``check_integrity`` (verify_index +
cross-tenant uploads) and ``find_leaks`` against an oracle computed from the inputs. Each mutant
is undone in ``finally`` (database objects are restored from migration 0007's own SQL).

Runs in-process (TestClient + retrieval service) against the TEST branch.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import secrets
import uuid
from pathlib import Path

import pytest
from sqlalchemy import and_, text

from app.auth.policy import AuthUser
from app.db.session import admin_transaction
from app.retrieval import authorize as authz
from app.retrieval.acl_filter import build_acl_clause
from app.uploads import api as uploads_api
from app.uploads import service as upload_service
from tests.security.dynamic import PREFIX, purge
from tests.security.oracle import Persona, find_leaks
from tests.security.test_mutations import _svc

pytestmark = [pytest.mark.neon, pytest.mark.security]

PW = "Mut-" + secrets.token_urlsafe(12)  # throwaway test-branch password
_M7 = Path(__file__).resolve().parents[2] / "migrations" / "versions" / "0007_self_service.py"


def _m7():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("m0007", _M7)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture()
def two_companies(app_role, client):
    """Companies A and B (admin + employee each), registered through the API."""
    purge(PREFIX + "mut-")
    out = {}
    for k in ("a", "b"):
        slug = f"{PREFIX}mut-{k}-{secrets.token_hex(2)}"
        r = client.post("/tenants/register", json={"company_name": f"Mutant {k.upper()}", "slug": slug,
                        "admin_email": f"admin@{slug}.example", "admin_password": PW})  # fmt: skip
        assert r.status_code == 201, r.text
        reg = r.json()
        h = {"Authorization": f"Bearer {reg['access_token']}"}
        e = client.post("/admin/users", headers=h, json={"email": f"emp@{slug}.example",
                        "roles": ["employee"], "password": PW}).json()  # fmt: skip
        tid = uuid.UUID(reg["tenant_id"])
        out[k] = {
            "h": h, "tenant_id": tid, "slug": slug,
            "admin": AuthUser(id=uuid.UUID(reg["user_id"]), tenant_id=tid, roles=frozenset({"admin"})),
            "emp": AuthUser(id=uuid.UUID(e["id"]), tenant_id=tid, roles=frozenset({"employee"})),
        }  # fmt: skip
    yield out
    purge(PREFIX + "mut-")


def _integrity(tenant_ids: list[uuid.UUID]) -> list[str]:
    """The suite's post-run integrity checks, restricted to these companies."""
    from app.admin.verify import verify_index

    with admin_transaction() as s:
        problems = [f"verify_index forbidden state {v.name}" for v in verify_index(s).violations
                    if v.count]  # fmt: skip
        wrong = s.execute(text(
            "SELECT d.id FROM documents d JOIN users u ON u.id = d.uploaded_by "
            "WHERE d.tenant_id = ANY(:t) AND d.tenant_id <> u.tenant_id"), {"t": tenant_ids}).scalars().all()  # fmt: skip
    return problems + [f"forbidden cross-tenant upload {d}" for d in wrong]


def _verdict(problems: list[str]) -> dict:
    v = {"problems": problems, "passes": not problems}
    print("MUTATION verdict:", v)
    return v


# ------------------------------------------------------------------ (i) trust form tenant_id
def check_form_tenant(client, c) -> dict:  # type: ignore[no-untyped-def]
    """Family (a) as the suite runs it: A's admin smuggles B's tenant_id in the upload form."""
    a, b = c["a"], c["b"]
    r = client.post("/documents/upload", headers=a["h"],
                    data={"title": "ATTACK smuggle", "classification": "public", "tenant_id": str(b["tenant_id"])},
                    files={"file": ("x.txt", b"Smuggled content that is long enough to index.", "text/plain")})  # fmt: skip
    problems = [] if r.status_code == 422 else [f"expected (422,), got {r.status_code}"]
    with admin_transaction() as s:
        landed = s.execute(text("SELECT id FROM documents WHERE tenant_id = :b AND title = 'ATTACK smuggle'"),
                           {"b": b["tenant_id"]}).scalars().all()  # fmt: skip
    problems += [f"forbidden document {d} written into another tenant" for d in landed]
    return _verdict(problems + _integrity([a["tenant_id"], b["tenant_id"]]))


def test_intact_upload_rejects_form_tenant(two_companies, client):
    assert check_form_tenant(client, two_companies)["passes"]


def test_mutant_i_handler_trusts_form_tenant_fails_the_suite(two_companies, client, monkeypatch):
    real_begin = upload_service.begin

    def begin(user, meta, **kw):  # type: ignore[no-untyped-def]
        forged = getattr(begin, "tenant", None)
        if forged:  # the bug: identity re-bound to the tenant named by the client
            user = dataclasses.replace(user, tenant_id=uuid.UUID(forged))
        return real_begin(user, meta, **kw)

    orig_validate = uploads_api.UploadForm.model_validate

    def validate(raw, *a, **k):  # type: ignore[no-untyped-def]
        # the bug: the form's tenant_id is accepted (taken out before validation) and used
        begin.tenant = raw.pop("tenant_id", None) if isinstance(raw, dict) else None  # type: ignore[attr-defined]
        return orig_validate(raw, *a, **k)

    monkeypatch.setattr(uploads_api.UploadForm, "model_validate", staticmethod(validate))
    monkeypatch.setattr(upload_service, "begin", begin)
    v = check_form_tenant(client, two_companies)
    assert not v["passes"], v
    # the database refused the forged identity on its own (defence in depth): nothing landed
    assert not any("written into another tenant" in p for p in v["problems"]), v


# ------------------------------------------------------------------ (ii) no same-tenant check
def check_foreign_allowed_user(client, c) -> dict:  # type: ignore[no-untyped-def]
    a, b = c["a"], c["b"]
    r = client.post("/documents/upload", headers=a["h"],
                    data={"title": "ATTACK users", "classification": "confidential",
                          "allowed_users": str(b["emp"].id)},
                    files={"file": ("x.txt", b"Content shared with a user of another company.", "text/plain")})  # fmt: skip
    problems = [] if r.status_code == 422 else [f"expected (422,), got {r.status_code}"]
    return _verdict(problems + _integrity([a["tenant_id"], b["tenant_id"]]))


def test_intact_rejects_foreign_allowed_user(two_companies, client):
    assert check_foreign_allowed_user(client, two_companies)["passes"]


def test_mutant_ii_skip_same_tenant_check_fails_the_suite(two_companies, client):
    m7 = _m7()
    check = m7.BEGIN_UPLOAD[
        m7.BEGIN_UPLOAD.index("    -- every named user") : m7.BEGIN_UPLOAD.index(
            "    INSERT INTO public.documents"
        )
    ]
    mutant = m7.BEGIN_UPLOAD.replace(check, "    -- MUTANT: same-tenant check removed\n")
    assert mutant != m7.BEGIN_UPLOAD
    try:
        with admin_transaction() as s:
            s.execute(text(mutant))
        v = check_foreign_allowed_user(client, two_companies)
    finally:
        with admin_transaction() as s:
            s.execute(text(m7.BEGIN_UPLOAD))
    assert not v["passes"], v
    assert any("document_user_from_other_tenant" in p for p in v["problems"]), v
    with admin_transaction() as s:  # clean the bad row before the next test's verify_index
        s.execute(text("DELETE FROM documents WHERE title = 'ATTACK users'"))


# ------------------------------------------------------------------ (iii) ready filter off
CANARY = "UNFINISHED-UPLOAD-" + secrets.token_hex(4).upper()


@pytest.fixture()
def unfinished(two_companies):
    """A document stuck in 'processing' whose chunks were (partly) written: nobody may see it."""
    a = two_companies["a"]
    doc = upload_service.begin(a["admin"], upload_service.UploadMeta("Draft merger memo", "public", [], []),
                               size=10, content_type="text/plain", filename="m.txt",
                               request_id=str(uuid.uuid4()))["doc_id"]  # fmt: skip
    from app.ingestion.embedder import HashEmbedder

    body = f"Draft merger memo, unfinished upload. Codename {CANARY}. Not yet approved."
    vec = HashEmbedder(384).embed_documents([f"Draft merger memo\n\n{body}"])[0]
    with admin_transaction() as s:
        s.execute(text(
            "INSERT INTO chunks (chunk_id, doc_id, tenant_id, title, chunk_index, text, embedding, "
            "classification, allowed_roles, allowed_users, acl_version) VALUES (gen_random_uuid(), "
            ":d, :t, 'Draft merger memo', 0, :x, CAST(:v AS vector), 'public', '{}', '{}', 1)"),
            {"d": doc, "t": a["tenant_id"], "x": body, "v": "[" + ",".join(map(str, vec)) + "]"})  # fmt: skip
    yield doc
    with admin_transaction() as s:
        s.execute(text("DELETE FROM chunks WHERE doc_id = :d"), {"d": doc})


def check_unfinished(svc, c, doc: str) -> dict:  # type: ignore[no-untyped-def]
    emp = c["a"]["emp"]
    p = Persona(key="a:emp", email="", user=emp, tenant="a", allowed_docs=frozenset(),
                forbidden_docs=frozenset({doc}), forbidden_canaries=(CANARY,))  # fmt: skip
    chunks, _, _ = svc.retrieve(f"What is the codename {CANARY} in the draft merger memo?", emp)
    return _verdict(find_leaks(p, retrieved_docs=[x.doc_id for x in chunks], cited_docs=(),
                               text="\n".join(x.text for x in chunks)))  # fmt: skip


def _no_ready_clause(user):  # type: ignore[no-untyped-def]
    full = build_acl_clause(user)
    return and_(*list(full.clauses)[:-1])  # the last conjunct is the Phase 6 ready EXISTS


def _loader_without_ready(session, ids):  # type: ignore[no-untyped-def]
    """Layer 3 mutant: every document looks 'ready' to the post-check.

    Injected through RAGService(doc_loader=...), which is how the service passes it on.
    """
    return {k: dataclasses.replace(v, status="ready")
            for k, v in authz.load_document_acls(session, ids).items()}  # fmt: skip


class _PolicyWithoutReady:
    """Recreate the 0004 chunks policy (no ready EXISTS) for the duration of a block."""

    def __enter__(self):  # type: ignore[no-untyped-def]
        m7 = _m7()
        old = m7.CHUNKS_POLICY.replace(
            "        AND EXISTS (SELECT 1 FROM documents d WHERE d.id = chunks.doc_id AND d.status = 'ready')\n",
            "",
        )
        assert old != m7.CHUNKS_POLICY
        self.m7 = m7
        with admin_transaction() as s:
            s.execute(text("DROP POLICY chunks_tenant_acl ON chunks"))
            s.execute(text(old))

    def __exit__(self, *exc):  # type: ignore[no-untyped-def]
        with admin_transaction() as s:
            s.execute(text("DROP POLICY chunks_tenant_acl ON chunks"))
            s.execute(text(self.m7.CHUNKS_POLICY))


def test_intact_system_hides_unfinished_document(two_companies, unfinished):
    assert check_unfinished(_svc(), two_companies, unfinished)["passes"]


def test_mutant_iii_ready_filter_disabled_fails_the_suite(two_companies, unfinished):
    with _PolicyWithoutReady():
        v = check_unfinished(_svc(acl_clause_fn=_no_ready_clause, doc_loader=_loader_without_ready),
                             two_companies, unfinished)  # fmt: skip
    assert not v["passes"] and any("retrieved forbidden doc" in p for p in v["problems"]), v


@pytest.mark.parametrize("layer", ["where_clause", "rls", "post_check"])
def test_ready_filter_each_layer_alone_is_enough(two_companies, unfinished, layer):
    """Two layers without the ready filter, the third with it: still hidden."""
    loader = authz.load_document_acls if layer == "post_check" else _loader_without_ready
    clause = build_acl_clause if layer == "where_clause" else _no_ready_clause
    svc = _svc(acl_clause_fn=clause, doc_loader=loader)
    if layer == "rls":
        v = check_unfinished(svc, two_companies, unfinished)
    else:
        with _PolicyWithoutReady():
            v = check_unfinished(svc, two_companies, unfinished)
    assert v["passes"], v
