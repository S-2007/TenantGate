# Requires GNU make. On Windows: `winget install GnuWin32.Make` or run the commands directly.
PY ?= python
LABEL ?= baseline_no_acl

.DEFAULT_GOAL := help
.PHONY: help up down run migrate migrate-test test test-unit lint fmt seed ingest verify app-role eval benchmark review policy-cov reconcile security demo benchmark-p6 run-selfservice

help:          ## list targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | sed -e 's/:.*## /	/' | awk -F'	' '{printf "  %-12s %s
", $$1, $$2}'

up:            ## build + start the API container (talks to Neon via .env)
	docker compose up --build -d

down:          ## stop the API container
	docker compose down

run:           ## run the API on the host with uvicorn (still talks to Neon)
	$(PY) -m uvicorn app.main:app --reload --port 8000

migrate:       ## apply migrations to the dev branch (DATABASE_URL_DIRECT)
	$(PY) -m alembic upgrade head

migrate-test:  ## apply migrations to the test branch (guarded against production)
	$(PY) -m alembic -x db=test upgrade head

test:          ## all tests; Neon-backed ones need TEST_DATABASE_URL (skipped locally if unset)
	$(PY) -m pytest -q

test-unit:     ## unit tests only (no database)
	$(PY) -m pytest -q tests/unit

lint:          ## ruff lint + format check (what CI runs)
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

fmt:           ## auto-format and auto-fix
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

eval:          ## Phase 1 eval as the row personas (LABEL=name; needs a running API for --mode http)
	$(PY) -m eval.run_eval --label $(LABEL)

benchmark:     ## Phase 5 benchmarks on throwaway Neon branches (CMD=latency|scale|neon|denied|all)
	$(PY) -m scripts.benchmark $(or $(CMD),all)

benchmark-p6:  ## Phase 6 benchmarks on throwaway Neon branches (CMD=upload|tenants)
	$(PY) -m scripts.benchmark_phase6 $(or $(CMD),tenants)

run-selfservice: ## run the API locally with self-service registration enabled (dev branch)
	REGISTRATION_ENABLED=true $(PY) -m uvicorn app.main:app --reload

reconcile:     ## check chunk ACLs against their documents (FIX=1 to repair)
	$(PY) -m scripts.reconcile_acls $(if $(FIX),--fix,)

security:      ## adversarial leakage suite + mutation meta-tests (Neon test branch)
	$(PY) -m pytest -q -s tests/security

demo:          ## the five demo scenarios against a running API (API=http://127.0.0.1:8000)
	bash scripts/demo.sh
