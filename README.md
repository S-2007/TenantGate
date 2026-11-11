# rag-platform: permission-aware multi-tenant RAG on Neon + pgvector

[![ci](https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/ci.yml)
<!-- replace OWNER/REPO with the GitHub repository once it exists -->

Status: **Phase 4, live permissions and attack testing.**
- **Phase 1:** naive baseline, with no auth and no ACL.
- **Phase 2:** identity from a verified JWT plus a DB re-check; ACL-carrying chunks enforced by
  the database; a SELECT-only app role.
- **Phase 3:** retrieval is protected three ways (ACL predicate inside the vector query, Postgres
  RLS, post-retrieval `can_access` re-check), with an append-only audit row per `/ask`.
- **Phase 4:**
  - ACL, role and activation changes are one atomic DB transaction, so they take effect on the
    very next query (`PATCH /admin/...`).
  - A data-driven adversarial suite (330 attacks, 15 families) runs in CI and prints `LEAKS: 0`.
  - Mutation meta-tests prove the suite fails when protections are broken.
- See `docs/ACCESS_MODEL.md` and `docs/WALKTHROUGH.md` (curl).

## Setup (Windows / macOS / Linux)

```bash
python -m venv .venv            # keep the venv outside OneDrive/Dropbox if you can
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -e ".[dev]"
```

Fill in the empty secrets in `.env` (see the comments in the file). Then:

| Make target | Plain command |
|---|---|
| `make migrate` | `python -m alembic upgrade head` |
| `make migrate-test` | `python -m alembic -x db=test upgrade head` |
| `make lint` | `ruff check . && ruff format --check .` |
| `make test` | `python -m pytest -q` |
| `make seed` | `python -m scripts.seed` (dev users; password in git-ignored `data/seed_users.json`) |
| `make ingest` | `python -m scripts.ingest --prune` |
| `make verify` | `python -m scripts.verify_index` |
| `make reconcile` | `python -m scripts.reconcile_acls` (`--fix` repairs drift) |
| `make security` | `python -m pytest -q -s tests/security` (leak suite; see `tests/security/README.md`) |
| `make app-role` | `python -m scripts.create_app_role` (once per branch; rewrites `DATABASE_URL` in `.env`) |
| `make policy-cov` | `python -m pytest tests/unit/test_policy.py --cov=app.auth.policy --cov-branch` |
| `make review` | `python -m scripts.gen_eval review` |
| `make eval` | `python -m eval.run_eval --label baseline_no_acl` |
| `make benchmark` | `python -m scripts.benchmark` |
| `make run` | `python -m uvicorn app.main:app --reload` |
| `make up` | `docker compose up --build -d` (API only; the DB is always Neon) |

## First run (Phase 2)

```bash
python -m alembic upgrade head       # owner, direct URL: schema + rag_app role
python -m scripts.seed               # tenants, 24 dev users, 66 documents (idempotent)
python -m scripts.ingest --prune     # ACL-aware chunks (ACL read from the documents table)
python -m scripts.verify_index       # must print "ok": true
python -m scripts.create_app_role    # app now connects as the SELECT-only rag_app_login
python -m uvicorn app.main:app       # POST /auth/login, then POST /ask with the bearer token
```

## LLM providers and fallback

Answers use `LLM_MODEL` first, then each model in `LLM_FALLBACKS`, in order, whenever the current
model is rate-limited or down. The eval judge does the same with `JUDGE_MODEL` and `JUDGE_FALLBACKS`.
Default chain: Groq `openai/gpt-oss-120b` > `qwen/qwen3.8-27b` > `openai/gpt-oss-20b` > Gemini
`gemini-3.6-flash` > `gemini-3.5-flash-lite`. `GEMINI_API_KEY` is optional; without it the Gemini
entries are skipped. For a single-model eval (needed for a fair baseline comparison), run
`python -m eval.run_eval --label x --no-fallback`. See `docs/DECISIONS.md` D-016.

## Neon branches

- `main`: production. Tests never touch it. A guard verifies this through the Neon API.
- `dev`: the branch `DATABASE_URL` / `DATABASE_URL_DIRECT` point at.
- `test`: create it from `dev`. `TEST_DATABASE_URL` / `TEST_DATABASE_URL_DIRECT` point at it.
- `ci-*`: throwaway branches that GitHub Actions creates from `dev` and deletes on every run.
  Add these repo secrets: `NEON_API_KEY`, `NEON_PROJECT_ID`.

See `docs/DECISIONS.md` for the design rationale and `docs/RESULTS.md` for measured numbers.
