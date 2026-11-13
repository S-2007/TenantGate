"""Executes attacks against a live API and checks every response against the oracle."""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx
import jwt

from app.config import get_settings
from tests.security.attacks import AskAttack, HttpAttack
from tests.security.oracle import PASSWORD, Oracle, Persona, find_leaks

# Server-internal details that must never reach a client. (FastAPI's 422 bodies echo the client's
# own input, so the attacker's SQL text itself showing up there is not a leak.)
SQL_ERROR_MARKERS = ("syntax error at or near", "psycopg", "sqlalchemy", "traceback",
                     "postgresql://", "neon.tech", "programmingerror", "sqlstate")  # fmt: skip


@dataclass
class Finding:
    family: str
    attack: str
    persona: str | None
    status: int
    problems: list[str]
    request_id: str | None = None
    path: str = "/ask"  # Phase 6: only /ask requests write retrieval audit rows


LEAK_MARKERS = ("forbidden", "canary", "existence leak")  # data reached the wrong persona


def is_leak(problem: str) -> bool:
    """Data exposure, as opposed to a harness or expectation failure (e.g. a wrong status)."""
    return any(m in problem for m in LEAK_MARKERS)


@dataclass
class RunResult:
    findings: list[Finding] = field(default_factory=list)  # one per request
    controls: list[dict] = field(default_factory=list)
    audit_problems: list[str] = field(default_factory=list)

    @property
    def leaks(self) -> list[Finding]:
        """Requests whose response exposed data the persona may not see."""
        return [f for f in self.findings if any(is_leak(p) for p in f.problems)]

    @property
    def failures(self) -> list[Finding]:
        """Requests that did not behave as expected (status, errors, attack not exercised)."""
        return [f for f in self.findings if any(not is_leak(p) for p in f.problems)]


class Tokens:
    """Logs each persona in once; also builds the tampered tokens."""

    def __init__(self, client: httpx.Client, oracle: Oracle) -> None:
        self.client, self.oracle = client, oracle
        self._cache: dict[str, tuple[str, float]] = {}

    REFRESH_S = 600  # tokens live 15 min; long runs (~16 min for the full cross product) re-login

    def login(self, key: str) -> str | None:
        hit = self._cache.get(key)
        if hit and time.time() - hit[1] < self.REFRESH_S:
            return hit[0]
        p = self.oracle.personas[key]
        r = self.client.post(
            "/auth/login", json={"email": p.email, "password": p.password or PASSWORD}
        )
        if r.status_code != 200:
            return None
        tok = r.json()["access_token"]
        self._cache[key] = (tok, time.time())  # last writer wins; both tokens are valid
        return tok

    def tampered(self, recipe: str, key: str | None) -> str | None:
        s = get_settings()
        now = int(time.time())
        p = self.oracle.personas[key] if key else None

        def claims(**over):  # type: ignore[no-untyped-def]
            base = {"sub": str(p.user.id), "tenant_id": str(p.user.tenant_id), "roles": sorted(p.user.roles), "tv": 1,
                    "iat": now, "exp": now + 600, "jti": str(uuid.uuid4()), "iss": s.jwt_issuer,
                    "aud": s.jwt_audience}  # fmt: skip
            return base | over

        def b64(obj) -> str:  # type: ignore[no-untyped-def]
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

        if p is None:  # garbage / no_token recipes need no identity
            return "not.a.jwt" if recipe == "garbage" else None
        other_tenant = next(t for slug, t in self.oracle.tenant_ids.items() if slug != p.tenant)
        if recipe in ("edit_tenant_unsigned", "edit_roles_unsigned"):
            real = self.login(key)  # type: ignore[arg-type]
            head, _, sig = real.split(".")  # type: ignore[union-attr]
            edit = (
                {"tenant_id": str(other_tenant)}
                if recipe == "edit_tenant_unsigned"
                else {"roles": ["admin", "hr"]}
            )
            return f"{head}.{b64(claims(**edit))}.{sig}"
        if recipe == "wrong_secret":
            return jwt.encode(claims(roles=["admin"]), "x" * 64, algorithm="HS256")
        if recipe == "alg_none":
            return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims(roles=['admin']))}."
        if recipe == "expired":
            return jwt.encode(
                claims(iat=now - 7200, exp=now - 3600), s.jwt_secret, algorithm="HS256"
            )
        if recipe == "wrong_audience":
            return jwt.encode(claims(aud="another-api"), s.jwt_secret, algorithm="HS256")
        if recipe == "wrong_issuer":
            return jwt.encode(claims(iss="evil-issuer"), s.jwt_secret, algorithm="HS256")
        if recipe == "deactivated_user":  # a correctly signed token of a now-deactivated user
            return jwt.encode(claims(), s.jwt_secret, algorithm="HS256")
        if recipe == "signed_other_tenant":  # insider with the signing key claims another tenant
            return jwt.encode(claims(tenant_id=str(other_tenant)), s.jwt_secret, algorithm="HS256")
        if recipe == "garbage":
            return "not.a.jwt"
        return None  # no_token


def _response_text(r: httpx.Response) -> str:
    return r.text + "\n" + "\n".join(f"{k}: {v}" for k, v in r.headers.items())


def _check_ask(persona: Persona, r: httpx.Response) -> list[str]:
    problems: list[str] = []
    if r.status_code != 200:  # the attack was not exercised: a failure, never a silent pass
        problems.append(f"HTTP {r.status_code} (attack not exercised)")
        return problems + find_leaks(
            persona, retrieved_docs=(), cited_docs=(), text=_response_text(r)
        )
    body = r.json()
    return problems + find_leaks(
        persona,
        retrieved_docs=[x["doc_id"] for x in body["retrieved"]],
        cited_docs=[c["doc_id"] for c in body["citations"]],
        text=_response_text(r),
    )


def run_suite(
    base_url: str, oracle: Oracle, asks: list[AskAttack], https: list[HttpAttack],
    controls: list[tuple[str, str, str]], *, personas_per_question: int = 4, workers: int = 8,
    all_persona_families: frozenset[str] = frozenset(),
) -> RunResult:  # fmt: skip
    client = httpx.Client(base_url=base_url, timeout=120)
    tokens = Tokens(client, oracle)
    active = sorted(k for k, p in oracle.personas.items() if p.active)
    for k in active:
        tokens.login(k)

    jobs: list[tuple] = []
    for i, a in enumerate(asks):  # round-robin: every persona attacks ~equally often
        if a.family in all_persona_families:  # Phase 6: e.g. uploaded injection, every persona
            jobs += [("ask", a, k) for k in active]
            continue
        for j in range(personas_per_question):
            jobs.append(("ask", a, active[(i * personas_per_question + j) % len(active)]))
    jobs += [("http", h, h.persona) for h in https]

    bodies: dict[str, str] = {}

    def run(job) -> Finding:  # type: ignore[no-untyped-def]
        kind, atk, key = job
        if kind == "ask":
            r = client.post("/ask", json={"question": atk.question},
                            headers={"Authorization": f"Bearer {tokens.login(key)}"})  # fmt: skip
            return Finding(atk.family, atk.question, key, r.status_code,
                           _check_ask(oracle.personas[key], r), r.headers.get("x-request-id"))  # fmt: skip
        headers = dict(atk.headers)
        tok = tokens.login(key) if atk.token == "persona" else tokens.tampered(atk.token, key)
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        r = client.request(atk.method, atk.path, json=atk.json, params=atk.params, headers=headers,
                           files=atk.files, data=atk.data)  # fmt: skip
        problems = (
            [] if r.status_code in atk.expect else [f"expected {atk.expect}, got {r.status_code}"]
        )
        low = r.text.casefold()
        problems += [
            f"error detail {m!r} in body"
            for m in SQL_ERROR_MARKERS
            if m in low and r.status_code != 200
        ]
        problems += [f"forbidden string {x!r} in response" for x in atk.forbid
                     if x.casefold() in low]  # fmt: skip
        persona = oracle.personas.get(key) if key else None
        if persona is not None and atk.path == "/documents/mine":  # uploader metadata (D-045)
            persona = persona.for_own_uploads_listing(oracle.doc_titles)
        if persona is not None and atk.path.startswith("/admin/security/"):  # D-047
            persona = persona.for_admin_audit_view()
        if persona is not None:
            if atk.path == "/ask" and r.status_code == 200:
                problems += _check_ask(persona, r)
            else:
                problems += find_leaks(
                    persona, retrieved_docs=(), cited_docs=(), text=_response_text(r)
                )
        if r.status_code in (401, 403, 404, 422):
            if any(c["canary"].casefold() in low for c in oracle.canaries):
                problems.append("canary in an error body")
        bodies[atk.name] = r.text
        return Finding(
            atk.family,
            atk.name,
            key,
            r.status_code,
            problems,
            r.headers.get("x-request-id"),
            path=atk.path,
        )

    res = RunResult()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        res.findings = list(pool.map(run, jobs))
    # uniform 404s: every probe of a family must look exactly like its first one
    for h in https:
        if h.same_body_as and bodies.get(h.name) != bodies.get(h.same_body_as):
            res.findings.append(
                Finding(h.family, h.name, h.persona, 0, ["404 body differs (existence leak)"])
            )

    for key, question, doc in controls:
        r = client.post("/ask", json={"question": question},
                        headers={"Authorization": f"Bearer {tokens.login(key)}"})  # fmt: skip
        got = [x["doc_id"] for x in r.json().get("retrieved", [])] if r.status_code == 200 else []
        res.controls.append(
            {"persona": key, "expected_doc": doc, "status": r.status_code, "ok": doc in got}
        )
    client.close()
    return res


def check_audit(oracle: Oracle, res: RunResult) -> list[str]:
    """Every /ask audit row lists only chunks the persona may see (owner read, by request id)."""
    from sqlalchemy import text

    from app.db.session import admin_transaction

    asks = [
        f for f in res.findings
        if f.request_id and f.persona and f.status == 200 and f.path == "/ask"
    ]  # fmt: skip
    by_rid = {uuid.UUID(f.request_id): f for f in asks}
    problems: list[str] = []
    with admin_transaction() as s:
        chunk_doc = dict(s.execute(text("SELECT chunk_id, doc_id FROM chunks")).all())
        rows = s.execute(
            text("SELECT request_id, returned_chunk_ids, user_id FROM retrieval_audit "
                 "WHERE request_id = ANY(:ids)"),
            {"ids": list(by_rid)},
        ).all()  # fmt: skip
    seen = defaultdict(int)
    for rid, chunk_ids, user_id in rows:
        f = by_rid[rid]
        seen[rid] += 1
        p = oracle.personas[f.persona]  # type: ignore[index]
        if user_id != p.user.id:
            problems.append(f"audit row {rid} user {user_id} != {p.user.id}")
        for c in chunk_ids:
            if chunk_doc.get(c) not in p.allowed_docs:
                problems.append(f"audit row {rid} lists forbidden chunk {c} ({chunk_doc.get(c)})")
    missing = [rid for rid in by_rid if seen[rid] != 1]
    if missing:
        problems.append(f"{len(missing)} /ask requests without exactly one audit row")
    return problems


def summary(oracle: Oracle, asks, https, res: RunResult, seconds: float) -> dict:  # type: ignore[no-untyped-def]
    fam_q = Counter(a.family for a in asks) + Counter(h.family for h in https)
    fam_req = Counter(f.family for f in res.findings)
    fam_leak = Counter(f.family for f in res.leaks)
    fam_fail = Counter(f.family for f in res.failures)
    return {
        "attack_queries_total": len(asks) + len(https),
        "ask_questions": len(asks),
        "http_attacks": len(https),
        "requests_total": len(res.findings),
        "families": {
            k: {
                "queries": fam_q[k],
                "requests": fam_req[k],
                "leaks": fam_leak[k],
                "failures": fam_fail[k],
            }
            for k in sorted(fam_q)
        },  # fmt: skip
        "personas": sorted(oracle.personas),
        "personas_attacking": len({f.persona for f in res.findings if f.persona}),
        "leaks": len(res.leaks) + len(res.audit_problems),
        "leak_details": [f.__dict__ for f in res.leaks][:50]
        + [{"audit": p} for p in res.audit_problems[:50]],
        "failures": len(res.failures),
        "failure_details": [f.__dict__ for f in res.failures][:50],
        "positive_controls": res.controls,
        "positive_controls_ok": all(c["ok"] for c in res.controls),
        "status_codes": dict(Counter(f.status for f in res.findings)),
        "duration_s": round(seconds, 1),
    }
