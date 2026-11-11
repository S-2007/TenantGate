# Requires GNU make. On Windows: `winget install GnuWin32.Make` or run the commands directly.
PY ?= python
LABEL ?= baseline_no_acl

.PHONY: up down run migrate migrate-test test test-unit lint fmt seed ingest verify app-role eval benchmark review policy-cov reconcile security

up:            ## build + start the API container (talks to Neon via .env)
	docker compose up --build -d

down:
	docker compose down

run:           ## run the API on the host with uvicorn (still talks to Neon)
	$(PY) -m uvicorn app.main:app --reload --port 8000

migrate:       ## apply migrations to the dev branch (DATABASE_URL_DIRECT)
	$(PY) -m alembic upgrade head

migrate-test:  ## apply migrations to the test branch (guarded against production)
	$(PY) -m alembic -x db=test upgrade head

test:          ## all tests; Neon-backed ones need TEST_DATABASE_URL (skipped locally if unset)
	$(PY) -m pytest -q

test-unit:
	$(PY) -m pytest -q tests/unit

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

fmt:
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

seed:          ## validate corpus, then upsert tenants/users/documents (dev/test only; refuses in production)
	$(PY) -m scripts.seed

ingest:        ## (re)build the ACL-aware chunk index (owner, direct URL); run seed first
	$(PY) -m scripts.ingest --prune

verify:        ## audit every chunk against its document's tenant and ACL (exit 1 on any violation)
	$(PY) -m scripts.verify_index

app-role:      ## create/rotate the SELECT-only app login role and point DATABASE_URL at it
	$(PY) -m scripts.create_app_role

policy-cov:    ## can_access truth table with branch coverage
	$(PY) -m pytest -q tests/unit/test_policy.py --cov=app.auth.policy --cov-branch --cov-report=term-missing

review:        ## regenerate data/eval/REVIEW.md
	$(PY) -m scripts.gen_eval review

eval:
	$(PY) -m eval.run_eval --label $(LABEL)

benchmark:
	$(PY) -m scripts.benchmark

reconcile:     ## check chunk ACLs against their documents (FIX=1 to repair)
	$(PY) -m scripts.reconcile_acls $(if $(FIX),--fix,)

security:      ## adversarial leakage suite + mutation meta-tests (Neon test branch)
	$(PY) -m pytest -q -s tests/security
