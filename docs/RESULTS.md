# Results log

Every number produced by the project, with date and the exact command. Never edited after the
fact: new runs get new rows.

| Date | Phase/step | Metric | Value | Command | Notes |
|---|---|---|---|---|---|
| 2026-11-10 | P1-C | ingest dry-run: chunks / tokens per chunk / est. storage | 184 chunks from 66 docs; min 56, median 500, max 500 bge tokens; ~1.1 MB | `python -m scripts.ingest --dry-run` | CHUNK_SIZE=500, OVERLAP=50, bge-small-en-v1.5 tokenizer |
| 2026-11-10 | P1-B | corpus size | 66 docs (22/tenant; 12 public, 30 internal, 24 confidential; 42 .md / 24 .txt), 45,493 words (min 369, median 609, max 1301 per doc) | `python -m scripts.seed` + word-count script | 21 canaries, all verified unique to their tenant |
| 2026-11-10 | P1-D | eval set | 90 questions (57 factual, 21 multi_chunk, 12 unanswerable; 18 public / 48 internal / 24 confidential), 0 flagged by support checks | `python -m scripts.gen_eval review` | Drafted by Claude (subagents) with verbatim evidence, **not yet hand-reviewed** |
| 2026-11-10 | P1-D | OFFLINE PREVIEW, exact in-memory cosine (NOT Neon): hit@5 / recall@5 | 0.949 / 0.885 | `LLM_PROVIDER=fake python -m eval.run_eval --label offline_exact_fakellm --mode memory --judge fake` | Upper bound for HNSW retrieval. Real baseline on Neon still to run |
| 2026-11-10 | P1-D | OFFLINE PREVIEW: foreign_tenant_rate (share of retrieved chunks from another tenant) | **0.500** overall (0.637 internal-class questions, 0.089 public) | same | No ACL: half of what reaches the prompt belongs to other tenants. Phase 2 target = 0 |
| 2026-11-10 | P1-D | OFFLINE PREVIEW: correct / citation_ok / refusal_ok / false_refusal | 0.278 / 0.551 / 0.750 / 0.064 | same | FAKE extractive LLM + FAKE judge: plumbing check only, NOT answer quality |
| 2026-11-10 | P1-A | Neon schema (dev + test) | alembic 0001; pgvector 0.8.6; `embedding vector(384)`; HNSW `vector_cosine_ops` m=16 ef_construction=64; Postgres 18.6 | `python -m alembic upgrade head` and `python -m alembic -x db=test upgrade head` | Region ap-southeast-1. Guard resolved branches `dev` / `test` through the Neon API |
| 2026-11-10 | P1-A | Neon connect (warm compute), 4 URLs | 0.66–0.71 s first connect, 0 retries, TLS in use on all 4 (client side) | ad-hoc connect check via `app.db.session.connect_with_retry` | Compute was already awake. A true scale-to-zero cold start is not measured yet |
| 2026-11-10 | P1-A | tests | 83 passed, 0 skipped (4 Neon-backed integration tests included) | `python -m pytest -q` | After the D-015 fixes. Before them: 74 passed, 0 skipped |
| 2026-11-10 | P1-C | ingest into Neon dev | 66 docs, 184 chunks inserted, 0 stale; embed 24.8 s, write 15.2 s (CPU, bge-small) | `python -m scripts.ingest --prune` | Wall clock ~60 s including model load |
| 2026-11-10 | P1-C | latency benchmark, Neon dev via pooler | first connect 767 ms; first query incl. connect 2329 ms; `SELECT 1` p50/p95 395/481 ms; query embed p50/p95 43/97 ms; vector search top-5 p50/p95 430/593 ms (n=30) | `python -m scripts.benchmark` | Search ≈ `SELECT 1`, so latency is network round-trip (laptop → ap-southeast-1), not the HNSW index |
| 2026-11-10 | P1-C | `/ask` smoke test via uvicorn | in-corpus: correct answer with citation (18.2 s on the first request, incl. lazy model load); off-topic: exact refusal (1.1 s); empty question: 422 | `python -m uvicorn app.main:app` + curl | `/health` 200 healthy, rows=184, dim matches |
| 2026-11-10 | P1-D | Groq token cost per RAG answer | ~2.3K tokens (gpt-oss-120b, reasoning_effort=low) | from Groq 429 message during `run_eval --resume` | 200K TPD fits about 81 answers plus judging. See D-015 |
| 2026-11-10 | P1-C | tests after D-016 (fallback chain + Gemini) | 107 passed, 0 skipped | `python -m pytest -q` | Gemini tested against a mocked HTTP transport only |
| 2026-11-10 | P1-C | live fallback check | gpt-oss-120b rate-limited, skipped 340 s (Groq retry hint); qwen/qwen3.8-27b answered "What is the meal per diem ... at Acme?" correctly ($45/day, cited) | `RAGService.ask` via `app.api.get_rag_service()` | Gemini entries skipped: GEMINI_API_KEY empty |
| 2026-11-10 | P1-C | live Gemini check | gemini-3.6-flash and gemini-3.5-flash-lite: RAG prompt answered correctly with [C1] (202 input tokens, 16 / 19 output tokens); judge (3.5-flash-lite) right answer = 1, wrong answer = 0; full pipeline with Gemini cited acme-expense-policy | ad-hoc calls via `build_llm("gemini", ...)`, `LLMJudge`, `RAGService.ask` | Free-tier Gemini limits not measured |
| 2026-11-10 | P1-D | **BASELINE `baseline_no_acl`** (Neon HNSW, top-5): hit@5 / recall@5 | **0.949 / 0.885** | `python -m eval.run_eval --label baseline_no_acl` then `--resume` twice; final CSV `eval/results/20261110T181045Z_baseline_no_acl.csv` | Same as the offline exact-search preview: HNSW loses nothing on this 184-chunk corpus |
| 2026-11-10 | P1-D | BASELINE: correct / citation_ok / refusal_ok / false_refusal | **0.456 / 0.641 / 0.917 / 0.154** | same | Answers: gpt-oss-120b 82, qwen3.8-27b 8 (fallback; the user chose to finish now instead of waiting for the 120b bucket). Judge: gpt-oss-20b on all 90. MIXED-model run, see D-017 |
| 2026-11-10 | P1-D | BASELINE per answer model: correct | gpt-oss-120b 0.427 (n=82); qwen3.8-27b 0.750 (n=8) | same | The n=8 qwen subset is too small to compare models |
| 2026-11-10 | P1-D | BASELINE: foreign_tenant_rate (share of retrieved chunks from another tenant) | **0.500** overall (internal 0.637, confidential 0.533, public 0.089) | same | Phase 2 target: 0.000 |
| 2026-11-10 | P1-D | BASELINE: answers citing another tenant's document | **32 / 90 (0.356)**; 31 of the 48 wrong answerable answers cite another tenant | analysis of the final CSV (cited_doc_ids vs tenant) | Main reason correct is low: "my" questions get answered with another company's policy |
| 2026-11-10 | P1-D | BASELINE: correct vs. context contamination (answerable questions) | no foreign chunk retrieved: 0.688 (n=16); ≥1 foreign chunk: 0.306 (n=62) | same | Correlation, not a controlled experiment. Phase 2 measures the causal effect |
| 2026-11-10 | P1-D | BASELINE: canary exposure | answers containing another tenant's canary: **0 / 90**; prompts that contained another tenant's canary document: **32 / 90** | analysis of final CSV vs `data/corpus/canaries.json` (21 canaries) | Benign questions did not trigger the leak, but the data reached the LLM 32 times. Phase 2 leakage suite must use adversarial queries |
| 2026-11-11 | P2-A | `can_access` truth table | 288 combinations (12 role sets x same/other tenant x active/inactive x 6 ACL shapes) + 10 named-rule tests, all pass; **100 % line and branch coverage** of `app/auth/policy.py` (56 stmts, 16 branches) | `python -m pytest tests/unit/test_policy.py --cov=app.auth.policy --cov-branch` | Expected values written independently of the implementation |
| 2026-11-11 | P2-A | seed idempotency (Neon dev) | run 1: 3 tenants / 24 users / 66 documents inserted; run 2: 0 inserted, 0 updated, 3 / 24 / 66 unchanged, 0 ACL bumps, 0 password resets | `python -m scripts.seed` (twice) | Same counts on both runs |
| 2026-11-11 | P2-A | seed production guard | exit 2, "Refusing to seed dev users: ENV=production", no DB access | `ENV=production python -m scripts.seed` | Also refuses on the Neon default branch (API guard) |
| 2026-11-11 | P2-A | app login role on dev | `rag_app_login`: superuser false, bypass_rls false, create_role false, create_db false, member of rag_app, owns 0 tables | `python -m scripts.create_app_role` | DATABASE_URL now uses this role (pooled) |
| 2026-11-11 | P2-B | full re-ingest into Neon dev (ACL-aware) | 66 docs, 184 chunks inserted, 0 stale; embed 26.4 s, write 10.9 s | `python -m scripts.ingest --prune` | Phase 1 chunks were deleted by migration 0003 (D-020) |
| 2026-11-11 | P2-B | verify_index on real data | ok = true; 184 chunks, 66 documents; all 9 violation checks = 0; documents_without_chunks = 0 | `python -m scripts.verify_index` | The corrupted-row case is covered by integration tests (exit 1 path) |
| 2026-11-11 | P2 | migrations + full suite on a **fresh throwaway Neon branch** | reset schema, `alembic upgrade head` (0001 to 0003, creates `rag_app`), **521 passed**; branch deleted (HTTP 200) | scratchpad script emulating CI (Neon API branch, `scripts.reset_test_schema --yes`, `alembic -x db=test upgrade head`, `pytest -q`) | Same steps as `.github/workflows/ci.yml`; GitHub CI itself not run yet |
| 2026-11-11 | P2-C | live API as `rag_app_login` | /health 200; /ask without token 401; login 200 (expires_in 900); /auth/me roles from DB; wrong password 401; deactivated 401 (same body); body `tenant_id` gives 422 + `suspicious_request` log; /ask with token 200 | `uvicorn app.main:app` + httpx script | Retrieval still unfiltered: acme employee got Northwind's "25 days" (cited northwind-leave-policy), Phase 3 target |
| 2026-11-11 | P3-A | equivalence: Python `can_access` == SQL WHERE == RLS only | equal for every user; 34 users x 256 chunks = 8,704 pairs (real corpus + test world), plus the documents table | `pytest tests/integration/test_retrieval_security.py::test_python_policy_equals_where_clause_equals_rls_only` | Covers all roles incl. contractor, inactive users, user-specific docs |
| 2026-11-11 | P3-A | narrow access top-k (HNSW forced, ef_search=10, contractor sees 8 of 256 chunks) | iterative_scan=off: **< 5 rows**; strict_order: **5 rows**, all from the one public doc, distance-ordered | `pytest ...::test_narrow_access_user_still_gets_top_k_with_iterative_scan` | EXPLAIN asserted to use ix_chunks_embedding_hnsw |
| 2026-11-11 | P3-A | post-filter vs pre-filter (salary-like query, employee) | unfiltered top-10 all forbidden: post-filter **0** results; pre-filter **5** allowed results | `pytest ...::test_post_filtering_returns_nothing_where_pre_filtering_returns_best_allowed_5` | Interview example |
| 2026-11-11 | P3-A | RLS-only / no settings / settings scope | RLS without WHERE returns only permitted rows; no settings returns 0 rows (chunks and documents); settings do not survive the transaction; malformed id errors; app role cannot disable RLS | `pytest tests/integration/test_retrieval_security.py -k rls` | |
| 2026-11-11 | P3-B | fault injection: WHERE removed AND RLS bypassed | forbidden chunks reached layer 3 and were all dropped; none of their doc ids or text in the fake LLM's prompt; SECURITY_ALERT logged; acl_violations_total increased | `pytest ...::test_fault_injection_where_and_rls_both_off_second_check_still_holds` | |
| 2026-11-11 | P3-B | stale ACL | chunk with old acl_version dropped (reason stale_acl), acl_stale_chunks_total +1, ACL_RESYNC_NEEDED logged; re-ingest restores it | `pytest ...::test_stale_acl_chunk_is_dropped_and_counted` | |
| 2026-11-11 | P3-C | audit: one row per /ask, append-only, admin endpoint | rows for ok / denied (401, 422) / error (502); app role UPDATE/DELETE/TRUNCATE denied; owner blocked by trigger; cross-tenant insert blocked by RLS; admin matrix 401/403/404/404/200; 30-day window; keyset pagination without duplicates | `pytest tests/integration/test_audit.py` | |
| 2026-11-11 | P3 | test totals | 508 unit + 74 Neon integration, all pass | `python -m pytest -q` | see final row for the full-suite run |
| 2026-11-11 | P3-A | **EVAL `secured_prefilter` vs `baseline_no_acl`** (http mode, logged in as each row's persona) | hit@5 0.949 to **0.974**; recall@5 0.885 to **0.942**; correct 0.456 to **0.878**; citation_ok 0.641 to **0.962**; refusal_ok 0.917 to **1.000**; false_refusal 0.154 to **0.026**; foreign_tenant_rate 0.500 to **0.000** | `python -m eval.run_eval --label secured_prefilter --mode http --base-url http://127.0.0.1:8000` then `--compare eval/results/20261110T181045Z_baseline_no_acl.csv eval/results/20261110T195514Z_secured_prefilter.csv` | **Model confound:** answers were qwen3.8-27b 75, gpt-oss-20b 10, gpt-oss-120b 5 (120b daily bucket spent); baseline was 82 x 120b. Judge gpt-oss-20b in both |
| 2026-11-11 | P3-A | eval control for the model confound | on the 16 answerable questions whose baseline retrieval had **no** foreign chunk: correct 0.688 (baseline) vs 0.688 (secured) | analysis of both CSVs | The gain comes from removing cross-tenant context, not from the model swap (n=16: evidence, not proof). A single-model rerun with `--no-fallback` is still recommended |
| 2026-11-11 | P3-A | secured eval leak checks | answers citing another tenant: 0/90 (baseline 32); prompts containing another tenant's canary doc: **0/90** (baseline 32); answers with a foreign canary: 0/90 | analysis of the secured CSV vs `data/corpus/canaries.json` | |
| 2026-11-11 | P3-C | curl walkthrough on dev (`docs/WALKTHROUGH.md`) | acme employee: "22 days" citing acme-leave-policy; admin sees who retrieved it (request id matches the X-Request-ID header); employee 403; northwind admin 404 for an acme doc, same body as a missing doc; body tenant_id 422; /metrics acl_violations_total 0 for every reason | commands in docs/WALKTHROUGH.md | Phase 2 gave the same employee Northwind's "25 days" |
| 2026-11-11 | P3 | migrations 0001 to 0005 + full suite on a **fresh throwaway Neon branch** | reset schema, `alembic upgrade head` (RLS, audit table, triggers, functions from zero), **582 passed**; branch deleted (HTTP 200) | CI-equivalent script (Neon API branch, `scripts.reset_test_schema --yes`, `alembic -x db=test upgrade head`, `pytest -q`) | GitHub Actions itself not run yet |
| 2026-11-11 | P4-A | revocation proofs (integration) | 16 passed: revoke then grant on the next query with the same token (checked by chunk ids); role change on the next query; deactivated user's valid token gives 401; admin matrix 401/403/404/404/200; 6 invalid changes rejected with nothing changed; cross-tenant grant 422; self-lockout 422; DB function refuses a non-admin even when called directly; drift blocked by the second check and repaired by reconcile; concurrency: 0 of the queries started after the revoke commit saw the document | `python -m pytest -q tests/integration/test_revocation.py` | |
| 2026-11-11 | P4-A | **time for a revoke to take effect** (50 revocations) | first query after the PATCH returned was denied **50/50** times (max 1 attempt); ms from PATCH return to first denied query: **p50 751, p95 842** (min 705, max 856) | `REVOKE_BENCH=1 python -m pytest -q tests/integration/test_revocation.py -k latency -s` | Atomic change, so no propagation delay; the time is one retrieval round trip to Neon ap-southeast-1 |
| 2026-11-11 | P4-B | **adversarial leakage suite** | **330 attack queries** (195 questions in 11 families + 135 crafted HTTP attacks in 4 families), **915 requests**, all **24 personas** sent requests, positive controls ok, **LEAKS: 0**, 0 audit problems | `python -m pytest -q -s tests/security/test_leakage.py` | Families: cross_tenant_names 18, canary_targeting 42, privilege_escalation 20, prompt_injection 15, jailbreak_roleplay 10, multilingual 10, encoded 18, sql_injection_question 12, indirect_injection 8, semantic_bait 30, response_probing 12, param_tampering 36, token_tampering 13, admin_probing 75, sql_injection_params 11 |
| 2026-11-11 | P4-B | mutation meta-tests (207 persona x query pairs each) | intact: 0 leaks, pass; (a) no tenant condition + RLS off + check off: **730 leaks, FAIL**; (b) owner role BYPASSRLS + no WHERE + check off: **1,019 leaks, FAIL**; (c) no set_config: 0 leaks (fail closed) but positive controls fail, **FAIL**; (d) RLS bound to the wrong identity: **1,270 leaks, FAIL**; WHERE only / RLS only / post-check only: 0 leaks, pass | `python -m pytest -q -s tests/security/test_mutations.py` | All 8 meta-tests pass |
| 2026-11-11 | P4 | **CI-equivalent run on a fresh throwaway Neon branch** (same steps and order as `.github/workflows/ci.yml`) | unit 509 passed; reset schema + migrations 0001 to 0006; integration 90 passed, 1 skipped (opt-in benchmark); security 9 passed, 1 skipped (semantic-cache placeholder), **LEAKS: 0**; reconcile gate drifted 0; branch deleted (HTTP 200) | scratchpad CI emulation script | GitHub Actions itself not run: the repo has no remote yet |
| 2026-11-11 | P4 | wall time of that run | unit 24 s, integration 11 m 52 s, security 10 m 48 s | same | Dominated by ~400 ms round trips to Neon ap-southeast-1 |
| 2026-11-12 | P4 | **GitHub Actions CI** (S-2007/TenantGate, push to main) | green: lint + test job (unit, throwaway Neon branch, reset + migrate, integration, security suite with LEAKS: 0 and mutation tests, reconcile gate, report published, branch deleted) | `.github/workflows/ci.yml` | about 50 min for the test job (timeout 60); dominated by runner (US) to Neon ap-southeast-1 round trips |
| 2026-11-12 | P5-A | **Benchmark environment** | Neon aws-ap-southeast-1, autoscaling 0.25 to 2 CU, Postgres 18.6, pgvector 0.8.6; client = developer laptop over WAN (warm round trip about 100 to 130 ms); bge-small-en-v1.5, top_k 5, HNSW m=16 ef_construction=64, ef_search=100, iterative_scan=strict_order | `python -m scripts.benchmark all` (each section on a throwaway branch of dev, deleted afterwards) | Raw JSON: docs/results.json, eval/results/*_bench_*.json |
| 2026-11-12 | P5-A2 | filtering latency overhead, 184 chunks, 540 timed queries per config after 50 warm-up (eval questions as their personas) | client p50/p95: no filter 131.91/162.23 ms; WHERE+RLS 129.72/160.05 ms (**delta -2.19 / -2.18 ms**). Server execution p50/p95: no filter 0.28/0.31 ms; WHERE+RLS 0.21/0.24 ms (**delta -0.07 ms**). WHERE only 0.15/0.32; RLS only 0.16/0.19 (server) | `python -m scripts.benchmark latency` | At this size the filter is free: the client time is the network round trip |
| 2026-11-12 | P5-A2 | post-filtering (unfiltered top-10, then can_access, keep 5), 90 queries | **72.2 %** of queries short of k=5; average 3.28 results | same | Pre-filter: 0 queries short of k |
| 2026-11-12 | P5-A5 | second-check drop rate | normal operation: **0 of 450** chunks dropped (0 %); fault injection (WHERE removed, RLS bypassed): **265 of 450 dropped (58.9 %)**, all forbidden | same | |
| 2026-11-12 | P5-A6 | **scale: 20,000 synthetic chunks** (2,000 docs, 3 tenants; 15 % public, 45 % internal, 40 % confidential), hash-384 embeddings | chunks table 81 MB, database 90 MB; load 112.6 s; HNSW build 8.0 s | `python -m scripts.benchmark scale` | Throwaway branch, deleted; within the free-tier storage limit |
| 2026-11-12 | P5-A6 | scale: filtering overhead, 300 timed queries per config | server execution p50/p95: no filter 1.15/1.68 ms; WHERE+RLS 1.26/1.48 ms (**delta +0.11 ms p50**); client p50/p95: 116.72/147.69 vs 121.32/183.47 ms (delta +4.6 / +35.8 ms); HNSW plan used in 100 % of queries | same | Client deltas are WAN noise at this scale; the server delta is the filter's real cost |
| 2026-11-12 | P5-A6 | scale: **recall@5 with the ACL filter** vs exact filtered top-5 (index scans disabled), 300 queries | iterative_scan strict_order, ef_search 100: **0.9993**; iterative_scan off, ef_search 40: **0.9347** | same | |
| 2026-11-12 | P5-A6 | scale: post-filtering (top-10, then can_access), 100 queries | **80 %** of queries short of k=5; average 2.03 results | same | |
| 2026-11-12 | P5-A2 | scale: without the GIN (allowed_roles, allowed_users) and tenant btree indexes (dropped in a rolled-back transaction) | WHERE-only server p50/p95 1.20/1.33 ms vs 1.18/1.48 ms with them: **no measurable effect** | same | The planner serves kNN + filter through HNSW; the ACL indexes serve audit/reconcile/verify queries, not this one |
| 2026-11-12 | P5-A7 | Neon connections (warm compute), 20 new connections + 100 warm SELECT 1 each | pooled: new connection p50 670.7 ms, SELECT 1 p50/p95 102.1/152.4 ms; direct: 671.7 ms, 107.9/141.5 ms | `python -m scripts.benchmark neon` | Connection setup = TLS + auth over WAN |
| 2026-11-12 | P5-A7 | **Neon cold start** after an API suspend (4 trials, alternating direct/pooled) | first query incl. connect: 1887.9, 2193.8, 1458.2, 1957.5 ms (**p50 1957.5 ms**); retries needed: **0** | same | connect_with_retry (4 attempts, backoff from 0.5 s) never needed a retry; the query-level retry (D-035) absorbed the one pooler blip seen in Phase 4 |
| 2026-11-12 | P5-A4 | **access-denied correctness**: each eval question asked by unauthorized personas | 102 unauthorized asks over 78 answerable questions (24 by a same-tenant persona without the role, 78 cross-tenant): **0** answers used a restricted source; 51 refusals | `python -m scripts.benchmark denied` | Service layer as the dev app role, fake LLM; a violation = any source doc retrieved or cited |
| 2026-11-12 | P5-A4 | **EVAL `final_secured`** (http mode, logged in as each row's persona) | hit@5 0.974; recall@5 0.942; correct 0.867; citation_ok 0.923; refusal_ok 1.000; false_refusal 0.038; foreign_tenant_rate 0.000; answers: gpt-oss-120b 83, qwen3.8-27b 7; judge gpt-oss-20b on all 90 | `python -m eval.run_eval --label final_secured --mode http --base-url http://127.0.0.1:8000`; CSV `eval/results/20261112T095954Z_final_secured.csv` | Nearly the same model mix as the baseline (82/8) |
| 2026-11-12 | P5-A4 | final vs baseline, **same model** (rows answered by gpt-oss-120b in both runs, n=82) | correct 0.427 to **0.866**; foreign_tenant_rate 0.495 to **0.000** | analysis of both CSVs | Control: the 15 of those rows whose baseline retrieval had no foreign chunk: correct 0.667 in both runs. The gain is the removed cross-tenant context |
| 2026-11-12 | P5-A1 | **full adversarial suite, full cross product** | **330 attack queries** (15 families) as all **24 personas**: **4,230 requests**, **LEAKS: 0**, other failures 0, positive controls ok; status codes 200 x 4119, 404 x 77, 422 x 18, 401 x 13, 403 x 3; 1013 s | `SECURITY_PERSONAS_PER_QUESTION=21 python -m pytest -q -s tests/security/test_leakage.py` | First attempt reported 122 "leaks": a harness bug (tokens expired after 15 min, see D-041), fixed and rerun; 0 of the 122 were data exposure |

## Final results (Phase 5)

Every number below is in the log above, with its date and exact command, and in
`docs/results.json`. The README copies these two blocks verbatim (`tests/unit/test_docs_sync.py`).

<!-- BEGIN:final-results -->
| Metric | baseline_no_acl (no ACL) | secured_prefilter (Phase 3) | final_secured (Phase 5) |
|---|---:|---:|---:|
| hit@5 | 0.949 | 0.974 | 0.974 |
| recall@5 | 0.885 | 0.942 | 0.942 |
| correct (LLM judge) | 0.456 | 0.878 | 0.867 |
| citation_ok | 0.641 | 0.962 | 0.923 |
| refusal correctness (unanswerable) | 0.917 | 1.000 | 1.000 |
| false refusals | 0.154 | 0.026 | 0.038 |
| share of retrieved chunks from another tenant | 0.500 | 0.000 | 0.000 |
| answers by gpt-oss-120b / qwen3.8-27b / gpt-oss-20b | 82 / 8 / 0 | 5 / 75 / 10 | 83 / 7 / 0 |

Same model (gpt-oss-120b in both runs, n = 82): correct 0.427 → 0.866. Control (15 of those rows
whose baseline retrieval was already clean): 0.667 → 0.667, so the gain comes from removing
cross-tenant context, not from the model.

| Measure | Result |
|---|---|
| Cross-tenant leaks, full adversarial suite | 0 in 4,230 requests (330 attack queries × 24 personas, 15 families) |
| Access-denied correctness | 0 of 102 unauthorized asks used a restricted source |
| ACL filtering overhead, server-side, 20,000 chunks | +0.11 ms p50 (1.15 → 1.26 ms) |
| ACL filtering overhead, client incl. WAN, 184 chunks, 540 queries | −2.19 ms p50 (131.91 vs 129.72 ms): within network noise |
| Recall@5 with the ACL filter, 20,000 chunks | 0.9993 (HNSW iterative scan) vs 0.9347 (iterative scan off) |
| Post-filtering (retrieve 10, then drop) | 72.2 % (184 chunks) / 80 % (20,000) of queries return fewer than 5; pre-filtering: 0 % |
| Revocation, 50 runs | first query after the PATCH denied 50/50; p50 751 ms / p95 842 ms PATCH-return → denied query (one round trip) |
| Second-check drops | 0 of 450 chunks in normal operation; 265 of 450 with WHERE + RLS disabled (fault injection) |
| Neon cold start (first query after suspend) | p50 1,957.5 ms over 4 runs; 0 connect retries needed |
| Neon pooled vs direct, warm SELECT 1 p50 | 102.1 ms vs 107.9 ms (WAN round trip from the developer laptop) |

Environment: Neon aws-ap-southeast-1, 0.25–2 CU autoscaling, Postgres 18.6, pgvector 0.8.6;
bge-small-en-v1.5 (384-d), top_k 5, HNSW m 16 / ef_construction 64 / ef_search 100.
<!-- END:final-results -->

<!-- BEGIN:leak-suite -->
330 attack queries in 15 families, including cross-tenant targeting, canary codenames,
privilege escalation, prompt injection, jailbreaks, multilingual and encoded variants, SQL
injection in questions and parameters, a poisoned document, semantic-similarity bait, existence
probing, parameter and token tampering, and admin-endpoint enumeration. Asked as all 24 personas
(4,230 requests): **LEAKS: 0**, 0 other failures, positive controls passed. CI runs the same
suite on every push, with 4 personas per question (915 requests). Mutation tests prove the suite
can fail: removing the protections produces 730, 1,019 or 1,270 leaks (or failed positive
controls when RLS fails closed), and each single layer alone still gives 0 leaks.
<!-- END:leak-suite -->

### Resume lines (every number measured above)

1. Built a multi-tenant RAG service (FastAPI, Neon Postgres + pgvector) with pre-retrieval ACL
   filtering, database row-level security and append-only audit logging, verified by a
   330-query adversarial test suite in CI with zero cross-tenant leaks and 0.11 ms server-side
   filtering overhead at 20,000 chunks.
2. Eliminated cross-tenant context in a multi-tenant RAG pipeline (share of retrieved chunks
   from other tenants 50 % → 0 %), raising answer correctness from 0.43 to 0.87 on the same LLM,
   while keeping recall@5 at 0.999 under ACL filters with pgvector iterative HNSW scans.
3. Made permission changes atomic across documents, vector chunks and the audit log (one
   Postgres transaction), so revocations applied on the very next query in 50 of 50 trials.
   Mutation tests proved the leakage suite fails (730–1,270 leaks) whenever the protections are
   removed.

Not measured, so not claimed: latency with the API deployed next to the database. All client
times above include the WAN round trip from a laptop.

### Limitations

- **LLM-side prompt injection** is not fully preventable by retrieval filtering. A poisoned
  allowed document can steer an answer. It can't exfiltrate data the model never received.
- **Embedding inversion:** stored vectors can leak information about their text to anyone with
  raw table access.
- **Latency side channels:** response time and result counts are not padded.
- **Key management:** HS256 with a shared secret in `.env`, and no rotation (RS256 plan in D-021).
- **No field-level redaction:** access is per document.
- **Neon free tier:** scale-to-zero cold starts (about 2 s), storage and branch limits, and shared
  compute. Free-tier LLM rate limits force model mixing in evaluations, which is disclosed per run.
| 2026-11-12 | P5-B5 | **clean-clone run** (exactly the files a commit contains: 202; no .env, no seed_users.json, no results) against a fresh EMPTY throwaway Neon branch, following the README quickstart literally | migrate, seed, ingest, verify_index (ok), create_app_role, API /health healthy; lint clean; **unit + integration 604 passed** (1 skipped: opt-in benchmark); **security suite + mutation tests 9 passed, LEAKS: 0**; eval pipeline ran end to end (fake LLM: hit@5 0.974, foreign_tenant_rate 0.000); reconcile drifted 0; second clean clone (quickstart + demo): **demo.sh ALL 5 SCENARIOS PASSED** | scratchpad clean-clone script (snapshot of `git ls-files -co --exclude-standard`, two throwaway branches, always deleted) | Reused the existing venv (same pinned versions); CI does a fresh install on every push. First demo attempt failed in the harness only: Windows resolved `bash` to WSL. README now says to use Git Bash |
