# Design decisions

Short entries, newest at the bottom. Each: decision, why, alternatives considered.

---

## D-001 One database: Neon serverless Postgres + pgvector (Phase 1)
**Decision.** Tenants, users, documents, chunks + embeddings and the audit log all live in one Neon
Postgres database with the `vector` extension.
**Why.** The core security claim of the project is that the ACL filter runs *inside* the vector
query and that Postgres row-level security enforces the same rule a second time. That is only
possible if vectors and ACL metadata are in the same transactional engine: one `WHERE` clause, one
RLS policy, one transaction, one audit write. With a separate vector DB the ACL would have to be
copied into payload filters and kept consistent by hand (dual-write races: a revoked permission
can still be live in the vector store). Postgres also gives us real transactions for "permission
change takes effect on the next query".
**Alternatives.** Qdrant/Chroma/Weaviate + Postgres (better pure-ANN performance and filtering
ergonomics, but two sources of truth and no RLS); Pinecone (managed, but RLS and audit live
elsewhere). At our scale (~10^3–10^5 chunks) pgvector HNSW is more than fast enough.
**Why Neon specifically.** Serverless, free tier, and *branching*: every CI run gets a copy-on-write
branch that is created and deleted in seconds, so integration and leakage tests run against real
Postgres + pgvector instead of mocks or SQLite.

## D-002 Pooled vs direct connection strings (Phase 1)
**Decision.** `DATABASE_URL` = Neon **pooled** host (`-pooler`, PgBouncer in transaction mode), used
by the running app. `DATABASE_URL_DIRECT` = direct host, used only by Alembic and admin scripts.
Both forced to `sslmode=require` (weaker modes are rejected, not upgraded silently).
**Why.** The app opens many short transactions, which is what the pooler is for. Migrations need
session semantics (DDL, advisory locks, `CREATE EXTENSION`) that transaction-mode pooling does not
guarantee. Config rejects a `-pooler` host for the direct URL.
**Consequences we design around.** In transaction mode no session state survives between
transactions, so: (1) psycopg auto-prepared statements are disabled (`prepare_threshold=None`);
(2) every per-request value (later: tenant / user / role for RLS) is set with
`set_config(..., true)` / `SET LOCAL` in the *same* transaction as the query (`app.db.session.transaction()`).
**Alternatives.** Direct connection for everything (simpler, but exhausts Neon's connection limit
under load); app-side pool only (still needs the server pooler on serverless compute).

## D-003 Cold starts: small pool + pre-ping + connect retry (Phase 1)
**Decision.** SQLAlchemy engine with `pool_size=3, max_overflow=2, pool_pre_ping=True,
pool_recycle=300`, and physical connects go through `connect_with_retry` (4 attempts, 0.5/1/2 s
backoff, 10 s connect timeout). Connect latency is recorded (`CONNECT_STATS`, exposed in `/health`).
**Why.** Neon scales compute to zero; the first connection after idle can take seconds or fail once.
Pre-ping discards connections PgBouncer has dropped. Only the host is logged, never the DSN.

## D-004 Branching strategy (Phase 1)
**Decision.** `main` = production (never used by tests). `dev` = development branch the app and
scripts use. Tests use `TEST_DATABASE_URL(_DIRECT)` pointing at a separate non-default branch
(e.g. `test`, branched from `dev`). CI creates `ci-<run_id>-<attempt>` from `dev` per run, resets its
`public` schema so migrations are proven on an empty database, runs tests, and **always** deletes
the branch (`if: always()`).
**Guard.** Tests, test migrations and the schema-reset script call `assert_not_production`, which
resolves the URL's endpoint → branch through the Neon API and aborts if it is the default branch
or named main/master/prod/production. It **fails closed**: without `NEON_API_KEY` +
`NEON_PROJECT_ID` it refuses to run (a name heuristic on the URL is impossible because Neon hosts
are random endpoint ids).
**CI limits.** Pull requests from forks cannot read repository secrets, so they cannot create a Neon
branch; the `test` job is skipped for fork PRs (lint still runs) and a maintainer must re-run on a
branch in the main repo. Neon free tier limits branches and compute-hours, so CI branches are
short-lived (one job) and parented on `dev`, not on production data.

## D-005 LLM wrapper: Anthropic default, deterministic FAKE provider (Phase 1) - SUPERSEDED by D-013
**Decision.** `LLMClient.complete(system, user)` with two providers. Default model `claude-opus-5-5`.
`fake` is an extractive, rule-based responder that understands our prompt format, cites the block
it used, and refuses below an overlap threshold.
**Why.** CI and tests must not need an API key or network access to a model, and must be bit-for-bit
reproducible. The fake is good enough to exercise citation parsing, refusal handling and the eval
plumbing.
**Note on "temperature 0".** Current Claude models reject sampling parameters (`temperature`,
`top_p`), so the real provider cannot be made deterministic that way. We use low effort, tight
output formats, and the FAKE provider/judge wherever determinism is required (tests). Real-judge
scores should be read as having some run-to-run noise; we record them per run.
**Refusals.** The server-side refusal fallback is enabled (`fallbacks="default"`); a final
`stop_reason == "refusal"` is mapped to our standard "I don't know" text.

## D-006 Embedding interface; deterministic hash embedder for tests (Phase 1)
**Decision.** `Embedder` protocol (`dim`, `max_tokens`, `tokenizer`, `embed_documents`,
`embed_query`). Default `BAAI/bge-small-en-v1.5` (384-d, normalised, cosine). `EMBEDDING_MODEL=hash`
selects a feature-hashing bag-of-words embedder used in tests/CI.
**Why.** Swappable model; CI does not download ~130 MB of weights; tests are deterministic. The
dimension lives in config, the ORM model and migration 0001; a unit test asserts all three agree,
the embedder raises if the model's real dimension differs, and `/health` reports
`dim_matches_config` by reading `atttypmod` of the actual column.
**bge query instruction.** bge retrieval works best with the documented query prefix
("Represent this sentence for searching relevant passages: "); passages are embedded without it.

## D-007 Chunking: 500 tokens / 50 overlap, counted in the embedding model's tokens (Phase 1)
**Decision.** Fixed token windows of `CHUNK_SIZE=500` with `CHUNK_OVERLAP=50` (10%), where a
"token" is a word-piece of the embedding model's own tokenizer (via the fast tokenizer's offset
mapping). Chunk text is an exact character slice of the source.
**Why.** bge-small has a hard 512-token input window. Counting in another tokenizer (e.g. tiktoken
`cl100k`) generally gives fewer tokens than a BERT word-piece tokenizer on the same text, so a "500-token" chunk could silently lose its tail
at embedding time. Counting in the model's units guarantees the chunk fits (500 + [CLS]/[SEP]);
the title prefix we embed (`title\n\ntext`) can push the last few tokens past 512 on a full window,
which is acceptable because those tokens are also the first tokens of the next (overlapping) chunk.
Slicing by offsets (not decode) keeps case, numbers and formatting intact, which matters for exact
facts such as `$142,500`. 500 tokens is roughly a policy section: large enough to keep a rule and its
exceptions together, small enough that top-5 fits easily in the prompt. 50 overlap stops a fact
that straddles a boundary from being lost.
**Alternatives.** Recursive/heading-aware splitting (better boundaries, but our corpus is
intentionally messy and inconsistent in headings, which makes heading rules brittle); smaller
chunks of 200-300 tokens (higher precision, but multi-sentence answers get split; to be tested as an eval
ablation, not assumed); semantic chunking (expensive and non-deterministic).
**Ids.** `chunk_id = uuid5(fixed_namespace, f"{doc_id}#{chunk_index}")`: re-ingest is a pure
upsert, and a document that shrinks has its trailing chunks deleted.

## D-008 Embedding model: BAAI/bge-small-en-v1.5 (Phase 1)
**Why.** 384-d (small vectors keep Neon storage and HNSW memory low on the free tier: 384 x 4 B =
1.5 KB per chunk), strong English retrieval quality for its size on MTEB, runs on CPU in CI and in
the API container, permissive license. Vectors are L2-normalised so cosine is the right metric.
**Alternatives.** all-MiniLM-L6-v2 (same dim, faster, generally weaker retrieval); bge-base / e5-base
(768-d, better but doubles storage and latency); hosted embeddings (an API key and network call
per query plus a data-egress concern for confidential text). Swappable via the `Embedder`
interface; a dimension change requires a migration (enforced by a unit test and the health check).

## D-009 HNSW index: m=16, ef_construction=64, cosine ops (Phase 1)
**Why.** These are pgvector's defaults and are well-tested for collections of 10^3-10^6 rows;
recall at our size (~10^2-10^3 chunks) is effectively exact. Query-time `hnsw.ef_search` stays at
the default 40 (it must be >= k=5). HNSW over IVFFlat because it needs no training step/`lists`
tuning and keeps good recall as rows are inserted incrementally.
**Known issue to address in Phase 2.** HNSW with a `WHERE` filter is *post-filtering inside the
index scan*: the index returns ef_search candidates, then the filter removes the ones the user may not see,
which can return fewer than k rows for a highly selective ACL. Phase 2 must handle this
(`hnsw.iterative_scan`, available in pgvector >= 0.8, or a larger ef_search) and measure it.

## D-010 Service layer shape: no DB transaction held during the LLM call (Phase 1)
**Decision.** `RAGService.ask` embeds the query, opens a transaction only for the vector search,
closes it, then calls the LLM. The API and the eval harness both call this same service.
**Why.** LLM latency is seconds. Holding a pooled Neon connection for that long would exhaust the
small pool. One code path for API and eval means eval numbers describe what users get.
**Prompt.** Context passages are labelled `[C1]..[Cn]`; the model must cite labels, which we map
back to (doc_id, chunk_id). Unknown labels are dropped (no fabricated citations). The prompt says
passage text is data, not instructions (basic prompt-injection hygiene; the real defence is
that unauthorised text never reaches the prompt, from Phase 2 on).

## D-011 Synthetic corpus designed for leak detection (Phase 1)
**Decision.** 3 fictional tenants x 22 docs. Every tenant has the same policy topics (leave, expense,
incident response, remote work) with deliberately *different* numbers (e.g. PTO 22 / 25 / 20 days),
and every confidential doc carries a tenant-unique canary string (codenames, salary-band figures,
incident ids, customer names). `data/corpus/canaries.json` registers them; `scripts/seed.py`
verifies each canary appears in its own document and in no other tenant's text.
**Why.** A leak must be *provable by string match*, not by judgement. Overlapping topics with
different numbers make cross-tenant retrieval both likely (same vocabulary) and detectable (wrong
number in the answer). Eval questions never name the company, so in the no-ACL baseline only
the embedding can disambiguate the tenant. The baseline foreign-tenant rate (0.50 offline) shows
that a similarity score is not an access-control mechanism.
**ACL metadata** (classification, allowed_roles, allowed_users with placeholder keys such as
`acme:employee_1`) is in `manifest.json` from day one, although Phase 1 does not use it.

## D-012 Eval design: doc-level retrieval metrics, verbatim evidence, fail-closed judge (Phase 1)
**Decision.** Metrics are doc-level (hit@k, recall@k) because answers are judged per document and
chunk boundaries change with chunking ablations. Citation accuracy requires cited docs to be a subset of
the retrieved docs *and* to include a source doc. Refusal correctness is measured on unanswerable questions,
and the false-refusal rate on answerable ones (both matter: refusing everything is "safe" but useless).
`foreign_tenant_rate` measures cross-tenant exposure in the prompt.
**Evidence.** Every eval row carries verbatim quotes (sidecar `evidence.jsonl`). `gen_eval review`
flags rows whose quotes are not in the source text, whose reference numbers are not in the
sources, whose persona cannot read the sources, or whose classification label is wrong. This is
the guard against LLM-written references that look plausible but are not supported.
**Judge.** The LLM judge returns strict JSON, and malformed output counts as incorrect (fail closed).
A deterministic FakeJudge (key-fact matching) exists for tests. Real-judge scores must always be
reported with the judge model name (stored in `summary.json` meta).
**Exact-search reference mode.** `run_eval --mode memory` swaps only the vector search for exact
in-memory cosine. It gives an offline smoke test and an upper bound for HNSW recall.
**Caveat.** The questions were drafted by an LLM (Claude subagents) from the corpus and pass the
automatic checks, but still need a human pass (`data/eval/REVIEW.md` has a checkbox per row).

## D-013 Switch the real LLM provider from Anthropic to Groq free tier (Phase 1, supersedes D-005)
**Decision.** `LLM_PROVIDER=groq`, `LLM_MODEL=llama-3.3-70b-versatile`, key `GROQ_API_KEY`
(replaces `ANTHROPIC_API_KEY`), official `groq` SDK. The Anthropic provider and dependency were
removed. The FAKE provider and the `LLMClient` interface are unchanged, so tests and CI are unaffected.
**Why.** Project requirement: no paid services. Every other component was already free:
Neon free tier, local sentence-transformers embeddings, HuggingFace model download,
GitHub Actions.
**Upside.** Groq accepts `temperature=0` and `seed`, so answers and the LLM judge are now
(near-)repeatable, which is better for before/after eval comparisons than D-005 allowed.
**Trade-offs.** (1) Free-tier rate limits are small: per-minute request and token caps, plus daily
caps. One RAG prompt is ~3k tokens (5 x 500-token chunks), so a full eval run (90 answers + 90 judge
calls) is throttled by tokens per minute. The SDK retries 429s with `max_retries=6`, honouring
`retry-after`, and a run that still fails records the error per question instead of aborting.
(2) Answer quality is a different model from the original plan, so baseline numbers must always
be compared at the same `llm_model` (recorded in each `summary.json`). (3) The judge and the
generator are the same model (self-grading bias). Mitigations: reference answers are anchored in
verbatim evidence, and the judge prompt grades key facts only. A cross-model judge (e.g.
`openai/gpt-oss-120b`, also on Groq's free tier) is a cheap later check.
**Alternatives.** Local Ollama (free and unlimited, but slow on a 6 GB laptop GPU and not available
in CI); Google Gemini free tier (similar limits, different SDK); keeping Anthropic (paid).

## D-014 Groq free-tier model change: gpt-oss-120b answers, gpt-oss-20b judge (Phase 1, amends D-013)
**What changed.** Groq shut down `llama-3.3-70b-versatile` and `llama-3.1-8b-instant` on
2026-08-16 (Groq deprecations page). The free-tier text models left are `openai/gpt-oss-120b`,
`openai/gpt-oss-20b` and `qwen/qwen3.8-27b`. Defaults are now `LLM_MODEL=openai/gpt-oss-120b`
(Groq's recommended replacement, and qwen 3.6 was itself already deprecated) and a new optional
`JUDGE_MODEL=openai/gpt-oss-20b` (empty = reuse LLM_MODEL).
**Why a separate judge model.** Rate limits are per model (listed: 30 RPM, 1K RPD, 8K TPM,
200K TPD), so answers and grading use separate buckets. A different model also reduces
self-grading bias. Both models are the same family, though, so the bias is reduced, not removed.
**Reasoning models.** gpt-oss and qwen3 spend hidden reasoning tokens that count toward
`max_completion_tokens` and the rate limits. We send `reasoning_effort=low` (gpt-oss) or `none`
(qwen) and do not return the reasoning text. Caps were raised (answers 2048, judge 1024). An empty answer at the
token cap raises an error instead of being scored as a refusal, which would have inflated refusal
correctness.
**Daily token cap.** Estimated ~3k tokens per RAG answer x 90 questions is above 200K TPD (an
estimate, not measured yet), so `run_eval --resume <csv>` keeps finished rows, re-runs errored or
missing ones, and writes a combined CSV. Judge exceptions now fail closed (`correct=0`,
`JUDGE_ERROR`) and are re-run by `--resume`. `summary.json` records `judge_model` and `errored_rows`.
**Not verified yet.** No live Groq call has been made (no key in this environment). The qwen
`reasoning_effort="none"` value comes from the SDK's allowed values and has not been tried against the API.

## D-015 Tolerant citation parsing and honest error rows (Phase 1, changes earlier behaviour)
**Found in the first live Groq run (2026-11-10).** `openai/gpt-oss-120b` often writes citations as
`[ C4 ]`, `【C1】` (full-width brackets) or `[C1, C3]`. The parser only matched `[C1]`, so the
API returned no citations for those answers. On the same 90 rows, eval `citation_ok` was 0.321
with the old parser and 0.590 with the new one (re-scored from the stored answers).
This was a measurement bug, not a model failure.
**Change.** `app.generation.prompt` now accepts optional spaces, full-width brackets and comma or
semicolon lists, and exposes `cited_labels()`. This changes `/ask` output: answers in these
formats now return their citations. Unknown labels are still ignored.
**Eval changes.** (1) A pipeline error now records the provider's message (Groq's text names the
exhausted bucket and the retry time; `org_…` ids are redacted, the API key is never in it).
(2) The judge is not called for a pipeline error: there is nothing to grade, and the call wastes
judge quota. The row still counts as wrong. (3) `--resume` re-derives citation fields of kept rows
from the stored answer (label `Cn` = the n-th retrieved doc, which is stored in rank order), so a
resumed run is scored by one parser. (4) A run with errored rows prints `INCOMPLETE` and the exact
resume command. Errored rows also have no retrieved docs, so they lower retrieval metrics as well.
**Measured token cost.** One RAG answer is about 2.3K tokens on gpt-oss-120b (Groq 429 message:
"Requested 2340"). 81 answered questions used the 200K TPD bucket, which refills at about
139 tokens/min. A full 90-question run therefore needs about 1.1 days of budget, or one
`--resume` after a few hours. This supersedes the ~3K estimate in D-014.
**Verified.** The live Groq key, `openai/gpt-oss-120b`, `openai/gpt-oss-20b` and
`qwen/qwen3.8-27b` were confirmed on 2026-11-10 (models list plus real completions). This closes
the "not verified yet" note in D-014 for gpt-oss. qwen `reasoning_effort="none"` is still untried.

## D-016 Multi-model fallback chain: Groq (3 models) + Gemini free tier (2 models) (Phase 1)
**Problem.** One free-tier model's daily token bucket (200K TPD on Groq) covers about 81 RAG
answers, less than one 90-question eval, and `/ask` returns 502 once it is spent.
**Change.** `FallbackLLM` tries models in order and returns the first answer:
`LLM_MODEL` (groq `openai/gpt-oss-120b`), then `LLM_FALLBACKS` =
groq `qwen/qwen3.8-27b`, groq `openai/gpt-oss-20b`, gemini `gemini-3.6-flash`,
gemini `gemini-3.5-flash-lite`. The judge has its own chain: `JUDGE_MODEL`
(groq `openai/gpt-oss-20b`), then `JUDGE_FALLBACKS` = gemini `gemini-3.5-flash-lite`,
groq `qwen/qwen3.8-27b`. Rate limits are per model on Groq, so each model adds its own bucket.
Gemini is reached through the official `google-genai` SDK (v3) and a new optional
`GEMINI_API_KEY`. Without it, the Gemini entries are skipped with a warning that names the variable.
**Design choices.**
- *Ordered failover, not load balancing.* The first healthy model always answers, so answers stay
  as consistent as possible.
- *Cooldown per model.* A failed model is skipped until the provider's own retry hint ("try again
  in 11m5s", Gemini `retryDelay`), or a default per error kind (rate limit 60 s, server or connection
  30 s, bad request 10 min, auth 1 h, empty answer 0). This means a spent daily bucket is not hit
  on every request. Inside a chain the SDKs retry only twice, so failover is fast.
- *Fail closed.* The primary's key is required at startup. A malformed fallback entry is a startup
  error. Only `LLMError` triggers failover: a programming error is never hidden by trying the next model.
- *Provenance.* `LLMResponse.model` is the model that actually answered. Eval rows now record
  `answer_model` and `judge_model`. The summary has `answer_models` / `judge_models` counts and a
  per-model breakdown, and prints `MIXED MODELS` when more than one model answered.
**Effect on evals (the trade-off).** Numbers from different models are not comparable, so a
fallback-mixed run is not a clean baseline. `run_eval --no-fallback` uses only the primary models.
The reference `baseline_no_acl` is finished with `--no-fallback` (all 90 answers from
gpt-oss-120b, judged by gpt-oss-20b). Later phases should either do the same, or compare only
rows answered by the same model.
**Gemini settings.** Thinking level `low` is the one level that every current free-tier flash and
flash-lite model accepts (Google thinking docs, 2026-11-10). Thought tokens count toward
`max_output_tokens`, so an empty answer at the cap is an error. Gemini 3.6+ ignores
temperature/top_p/top_k and Google says to remove them, so temperature 0 is sent only to older models.
A fixed seed is always sent.
**Data-handling caveat.** Google's pricing page says free-tier content is "used to improve our
products"; the paid tier is not. Our corpus is synthetic, so this is acceptable here. With real
tenant data, Gemini must be the paid tier or be removed from the chain, because a fallback
silently sends tenant documents to a second provider. Phase 2+ must treat every provider in the
chain as a data processor.
**Verified 2026-11-10.** Live: `qwen/qwen3.8-27b` accepts `reasoning_effort="none"` and
`reasoning_format="hidden"`. With gpt-oss-120b rate-limited, the chain skipped it for 340 s (Groq
hint) and qwen answered the Acme per-diem question correctly with a citation. Gemini, live, the
same day: `gemini-3.6-flash` and `gemini-3.5-flash-lite` both accept the request (thinking level
LOW, seed, and temperature 0 on 3.5-lite) and answered a RAG prompt correctly with `[C1]`. As
judge, `gemini-3.5-flash-lite` returned valid JSON, with 1 for a right answer and 0 for a wrong
one. A full `RAGService.ask` with Gemini answering cited the right Acme document. Still unverified:
the free-tier Gemini rate limits, which are published only on the signed-in AI Studio page.
**Alternatives.** OpenRouter free models or Cerebras free tier (more providers, more keys, same
data caveat); a single paid model (simplest, not free); local Ollama (unlimited, but slow on this
laptop and not available in CI).

## D-017 The recorded baseline is a mixed-model run (Phase 1)
**What.** `baseline_no_acl` (final CSV `20261110T181045Z`) has 82 answers from
`openai/gpt-oss-120b` and 8 from `qwen/qwen3.8-27b`. The qwen answers are 8 of the 9 resumed rows
(northwind-q22..q30), answered through the D-016 fallback after the 120b daily bucket ran out. All 90 rows were judged by
`openai/gpt-oss-20b`.
**Why.** The user chose to finish the baseline immediately rather than wait about 3 hours for
the 120b bucket and use `--no-fallback`.
**Consequence.** Retrieval metrics (hit@5, recall@5, foreign_tenant_rate) do not depend on the
LLM and are unaffected. Generation metrics (correct, citation_ok, refusal_ok, false_refusal) are
91% gpt-oss-120b. For a clean before/after in Phase 2, compare per `answer_model` (the summary
prints `model=` rows), or re-run the 8 rows with `--no-fallback`. Per-model rows are reported in
RESULTS.md. The 8-row qwen subset is too small to draw conclusions about qwen.

## D-018 Phase 2 data normalisation to match the access model (changes Phase 1 data)
**Why.** The spec defines `internal` as readable by *any* employee of the tenant. Six runbooks were
`internal` with `allowed_roles = [admin, engineering]`. Under the spec's rule, every employee could
have read them, so the restriction would have silently disappeared.
**Changes.**
- Those 6 runbooks are now `confidential` with `[admin, engineering]`. The 3 eval rows that use
  them (acme-q24, lumen-q24, northwind-q23) are now classification `confidential`.
- Public and internal documents now have **empty** ACL lists, enforced by DB `CHECK`, `ChunkPayload`
  and the manifest validator. A role list that is ignored looks like protection without being
  protection.
- Manifest users have `roles: [...]` and `active`. The unused Phase 1 `employee_2` is the
  deactivated user, and `multi_1` (manager + finance) is the multi-role user: 8 users per tenant.
- A `contractor` role exists as a **non-employee** role. It is what makes public different from
  internal. No contractor is seeded (the spec asks for 6–8 users); the tests create one.
- `gen_eval review` now judges "persona can read source" with `can_access`. REVIEW.md was
  regenerated: 90 questions, 0 flagged.
**Effect on the Phase 1 baseline.** Retrieval and answers are unchanged. Only the
`class=` breakdown moves 3 rows from internal to confidential.

## D-019 Two database roles; the app runs as a SELECT-only login (Phase 2)
**Design.**
- `neondb_owner` (the migration role, used through `DATABASE_URL_DIRECT`) owns every table.
- `rag_app` is a NOLOGIN group role with `USAGE` on `public` and `SELECT` on tenants, users,
  documents and chunks. Nothing else.
- The API connects as `rag_app_login`, a LOGIN member of `rag_app`, created by
  `scripts/create_app_role.py`. That script also rewrites `DATABASE_URL` in `.env` to the pooled
  host. Tests create a throwaway `rag_app_t_*` login per session and drop it afterwards, so CI needs
  no extra secret.
- Integration tests prove that the app role cannot ALTER, DROP, CREATE, TRUNCATE, INSERT, UPDATE
  or DELETE, and cannot change roles.
**Behaviour change.** Seed, ingest and verify_index are admin jobs, so they now run as the owner
over the direct URL. Phase 1 ingest used the pooled app URL, which the SELECT-only role can no
longer write through.
**Neon specifics found while testing.**
1. A role created through the Neon console or API joins `neon_superuser`, which has `BYPASSRLS`.
   That would break Phase 3's RLS, so `rag_app` is created with SQL instead.
2. Neon's owner is not a superuser, and Postgres 16+ refuses `NOSUPERUSER`, `NOREPLICATION` and
   `NOBYPASSRLS` from a non-superuser, even though they are the default. The migration and the
   script therefore create roles with defaults and then **assert** the attributes, aborting if
   any is set.
3. Neon rejects pre-hashed SCRAM passwords ("only supports being given plaintext passwords"). The
   plaintext is therefore sent in the `CREATE/ALTER ROLE` statement. It is 256-bit random,
   restricted to URL-safe characters (checked before inlining, since DDL takes no bind
   parameters), never printed, and rotated by re-running the script.
**Alternatives.** A single owner role for everything (simplest, but the app could drop tables and
bypass RLS). Roles created through the Neon API (they would be in `neon_superuser`).

## D-020 ACL-carrying chunks: the database makes an unprotected chunk impossible (Phase 2)
**Constraints.**
- `tenant_id NOT NULL`.
- A composite FK `(doc_id, tenant_id) -> documents(id, tenant_id)`, so a chunk can't exist without
  its document or claim another tenant.
- An ACL-shape `CHECK`: confidential needs roles or users; public and internal must have none.
- Known roles only, and no NULL array elements.
- `visibility` is a `GENERATED ... STORED` column derived from `classification`
  (`tenant` / `employees` / `restricted`). The spec's "visibility marker" is interpreted as a coarse
  pre-filter key that can never disagree with the classification.
- `allowed_users` is `uuid[]` rather than the spec's `text[]`, so a non-id can't be stored.
**Indexes.** Btree on `tenant_id`; GIN on `allowed_roles` and `allowed_users`; HNSW is kept. A
separate btree on `doc_id` was *not* added: the existing `UNIQUE (doc_id, chunk_index)` btree has
`doc_id` as its leading column, so it already serves those lookups and the FK cascade.
**ACL values.** The chunk's ACL copy has to equal the document's, and no `CHECK` can express that
across tables. It is guaranteed at write time (ingestion reads the ACL from `documents`, and
`ChunkPayload` validates every row before the first write; any failure aborts the transaction and
exits 1), and audited by `scripts/verify_index.py`.
A trigger that copies the ACL was rejected. It would hide drift instead of reporting it, and
Phase 4's "permission change takes effect on the next query" needs an explicit, observable update
path (`acl_version`).
**Migration 0003** deletes the 184 Phase 1 chunks. They are derived data with no tenant. The
rebuild is `seed` then `ingest`. In a real production system this would be a backfill instead.

## D-021 JWT: HS256 now, RS256 later
**Now.** HS256 with `JWT_SECRET` (>= 32 chars, validated at startup). The config refuses any other
`JWT_ALGORITHM`, and the decoder accepts only a header `alg` that is in `{HS256}` *and* equals the
config value. So `none`, `HS512`, `RS256` and any case variant are rejected before signature checking.
- Claims: `sub, tenant_id, roles, iat, exp, jti, iss, aud`, all required.
- Lifetime: 15 min (config, allowed 1-60).
- Clock-skew leeway: 10 s.
- No refresh tokens and no revocation list. A short expiry plus the per-request DB check (D-022)
  bounds the damage of a stolen token: deactivating the user kills it on the next request.
**Switching to RS256** (needed once a separate service or gateway must verify tokens without being
able to mint them):
1. Generate an RSA-2048 or EC P-256 key pair.
2. Keep the private key in a secret store, used only by the issuer.
3. Publish the public key, ideally as a JWKS with a `kid`.
4. Add `RS256` to `ALLOWED_ALGORITHMS`, and sign and verify with separate keys (verify with the
   public key only).
5. Select the key by `kid` from the allowlisted set, never from a URL in the token.
6. Rotate by publishing the new `kid` before signing with it, then retiring the old one after the
   maximum token lifetime.
HS256 stays rejected by RS256 verifiers, which prevents the classic "public key used as HMAC
secret" confusion attack.

## D-022 The database, not the token, is the source of truth for identity
`get_current_user` verifies the token, then reloads the user by `sub` on every request. The
request is rejected if the user is missing or inactive, or if the DB `tenant_id` differs from the
token's. Roles are taken from the DB; the token's `roles` claim is informational and only logged
when it differs. The cost is one primary-key query per request, which is acceptable at this scale
and can be cached for a few seconds later if needed.
The benefit: demotion, deactivation and tenant moves take effect on the **next request**, not when
the token expires. Integration tests cover roles changed after issuance and deactivation after
login.

## D-023 Login hardening and known gaps
- **Passwords:** Argon2id with argon2-cffi defaults. Input is capped at 1024 characters, to bound
  hashing work.
- **One response for every failure:** an unknown email, a wrong password and a deactivated account
  all get the same 401 and the same body, and all run exactly one Argon2 verification. A dummy hash
  is computed at startup and verified for unknown emails, so timing does not reveal registered
  emails.
- **Logs:** the reason and user id go to `app.security`. Passwords and unknown emails are never
  logged.
- **Email:** trimmed and lower-cased. The database enforces lower-case and uniqueness.
- **Not done yet:** rate limiting and lockout (to pair with deployment); rehashing on parameter
  upgrade (the app role cannot `UPDATE users`, so this belongs to an admin job); MFA.

## D-024 Request hygiene: identity can't be asserted by the client
`/ask` and `/auth/login` use Pydantic models with `extra="forbid"`, so a body containing
`tenant_id`, `roles` or `user_id` gets a 422. The validation handler logs it as
`suspicious_request reason=forbidden_body_fields`, with field names only.
A middleware also logs, but ignores, identity-looking headers (`X-Tenant-Id`, `X-Roles`, ...) and
query parameters.
`GET /auth/me` was added: it shows the caller as the server sees it, and makes "roles come from the
DB" testable.
**Still open, on purpose (Phase 3).** Retrieval is not filtered by the caller yet. Seen live on
2026-11-11: an authenticated Acme employee asked "How many PTO days do I get per year?" and got
Northwind's "25 days", citing `northwind-leave-policy`. Phase 2 makes *who is asking* trustworthy;
Phase 3 must use it (pre-filter in the vector query, RLS, post-check with `can_access`).
These events go to the application log only; the append-only audit log is Phase 4.

## D-025 ACL pre-filter inside the vector query; HNSW iterative scan (Phase 3)
**Predicate.** `build_acl_clause(user)` (app/retrieval/acl_filter.py) is the WHERE clause of the
same statement that does `ORDER BY embedding <=> :q LIMIT k`. Nothing is retrieved and then
dropped. It keys on the stored `visibility` column (`tenant` / `employees` / `restricted`,
generated from `classification` in migration 0003):
`tenant_id = :tenant AND (visibility='tenant' OR (visibility='employees' AND :is_employee) OR
(visibility='restricted' AND (allowed_roles && :roles OR :user_id = ANY(allowed_users))))`.
- **Why a visibility column, not a marker in `allowed_roles`.** A generated column can't disagree
  with `classification`, and it keeps the ACL lists meaning only "who may read this confidential
  document" (D-018). Putting `employee` in `allowed_roles` as a marker would bring back the
  ambiguity D-018 removed.
- **Bound parameters.** Every value is a bound parameter: tenant, user id, roles (unknown roles
  dropped first) and a boolean `is_employee` computed from `EMPLOYEE_ROLES`. An inactive or
  malformed user gets `FALSE`.
- **Equivalence with `can_access`** is proven on all seeded users (real corpus + test world,
  34 users × 256 chunks = 8,704 pairs): Python policy over all rows == app-role query with WHERE
  == app-role query with RLS only. The documents table is checked the same way.
**pgvector caveat.** HNSW explores `hnsw.ef_search` candidates and the filter runs on those, so a
selective filter can return fewer than k rows. Fix: transaction-local
`set_config('hnsw.iterative_scan', 'strict_order', true)` (pgvector 0.8.6) and `hnsw.ef_search = 100`
(config `HNSW_EF_SEARCH` / `HNSW_ITERATIVE_SCAN`).
- `strict_order` keeps exact distance order. `relaxed_order` would be faster but needs a
  re-sort; at this scale order matters more.
- The test forces the HNSW plan (`enable_seqscan/bitmapscan/sort = off`, asserted via
  EXPLAIN). With iterative scan off and `ef_search=10`, a contractor (8 readable chunks of 256)
  gets **fewer than 5** rows. With `strict_order`, exactly 5, all from the one public document.
- On this small table the planner normally picks the tenant btree plus a sort, which is exact
  anyway. The setting matters once the index is used, so it is always set.
**Retrieve-then-drop vs pre-filter** (interview example, test-backed): for a salary-like query,
the unfiltered top-10 are all HR-only chunks. Post-filtering leaves an employee with **0**
results, while the pre-filtered query still returns the 5 best chunks that employee may read.

## D-026 Postgres RLS as an independent second layer (Phase 3)
- `ENABLE` + `FORCE ROW LEVEL SECURITY` on `chunks`, `documents` and `retrieval_audit`.
  `rag_app` has SELECT-only policies that mirror the same rule, using helper SQL functions
  `app_tenant_id()`, `app_user_id()`, `app_roles()` and `app_is_employee()`.
- These functions read `current_setting('app.*', true)`. The values are set by
  `apply_rls_context` with `set_config(name, value, true)` inside the request's own transaction.
  That is required with Neon's transaction-mode pooler: a session-level `SET` could leak to the
  next client on the same server connection. A test checks that settings don't survive the
  transaction.
- **Fail closed:**
  - no settings → `NULL` comparisons → no rows (tested: `count(*) = 0` on chunks and documents);
  - a malformed id → cast error;
  - an inactive user → no settings are set at all;
  - any role other than `rag_app` members has no policy → no rows.
- **Owner.** `neondb_owner` has `BYPASSRLS` (checked), so admin jobs (seed, ingest, verify,
  audit inspection) still see everything. This is deliberate: the owner is the migration and
  admin identity, not the app.
- **The app role can't switch RLS off.** `ALTER TABLE ... DISABLE/NO FORCE`, `DROP POLICY` and
  `SET row_security = off` all fail (tested).
- **Cost.** The policy functions are `STABLE` and SQL-inlinable; EXPLAIN shows them inlined.

## D-027 Post-retrieval re-verification and the AuthorizedChunk type (Phase 3)
`authorize_chunks` (app/retrieval/authorize.py) runs after retrieval, in the same transaction.
For each chunk it checks:
1. `can_access` on the chunk's own ACL columns;
2. the authoritative `documents` row, read under the same RLS context: it must exist, have the
   same tenant and the same `acl_version`, and `can_access` must pass on *its* ACL.
**On failure** the chunk is dropped, never repaired:
- **A violation** (`policy_denied`, `document_missing`, `tenant_mismatch`,
  `document_policy_denied`) logs one `SECURITY_ALERT {json}` line (event type, reason,
  request_id, user, tenant, chunk id, doc id; never text) and increments
  `acl_violations_total{reason}`.
- **`stale_acl`** is a freshness problem, not an attack. It logs `ACL_RESYNC_NEEDED`, increments
  `acl_stale_chunks_total`, and is fixed by re-ingesting that document (tested).
- In both cases the request answers from the chunks that passed.
**Structural.** `AuthorizedChunk` needs a module-private construction token, is immutable, and
its `repr` has no text. `build_user_message` and `parse_citations` raise `TypeError` for anything
else, so an unverified row can't become prompt context or a citation, even after a refactor that
forgets the check.
**Fault injection (tested).** With the WHERE clause replaced by `TRUE` and RLS bypassed (owner
session, no context), two bugs at once, forbidden chunks reach layer 3. All of them are dropped,
the prompt the fake LLM received contains none of their doc ids or text, alerts are logged and
the counter rises.
**Other leak paths.**
- The response carries only authorized chunks: ids, doc ids, titles, scores, never text.
- LLM, DB and unhandled errors return generic messages with the request id. Logs name only the
  exception class.
- `/health` no longer reports a row count, which RLS would hide anyway and which reveals index size.
- Per-user chat history is marked in `RAGService.ask` and deliberately not built.

## D-028 Metrics endpoint
`GET /metrics` serves Prometheus text format 0.0.4 from in-process counters:
`acl_violations_total{reason}`, `acl_stale_chunks_total`, `audit_write_failures_total` and
`ask_requests_total{status}`. Labels are fixed reason strings only, never user, tenant or doc ids,
which would be both a cardinality and a privacy problem. Counters are per process (reset on
restart). Multiple workers would need `prometheus_client` multiprocess mode or a push gateway.
The endpoint is unauthenticated, like most scrape targets. At deployment it must sit on an
internal network or behind auth.

## D-029 Request ids
Middleware gives every request a request id. A client `X-Request-ID` is accepted only if it is a
UUID; anything else is replaced, to prevent log and audit injection. The id is put in a
contextvar (so every log line shows `[req=...]`), echoed as the `X-Request-ID` response header,
returned in `/ask` bodies, and stored in the audit row. One id ties together the client, the logs,
the alerts and the audit trail.

## D-030 Append-only retrieval audit
`retrieval_audit` (migration 0005) has one row per POST /ask.
**Append-only, enforced three ways:**
1. **Grants:** `rag_app` has INSERT and SELECT only.
2. **Trigger:** a `BEFORE UPDATE OR DELETE` row trigger and a `BEFORE TRUNCATE` statement
   trigger raise. They bind the owner too (tested).
3. **RLS:** the app can read only its tenant's rows, and insert only rows for its own tenant, or
   unauthenticated rows with both ids NULL. A `CHECK` requires the user id and tenant id to be
   either both set or both NULL.
**Design choices.**
- **No foreign keys:** audit rows outlive the users, documents and tenants they mention.
- **Indexes:** GIN on `returned_doc_ids` (the endpoint queries with `@> ARRAY[:doc]`, which GIN
  serves; `= ANY` would not) and `(tenant_id, ts DESC)`.
- **Known limit:** the owner can still drop the trigger. Tamper-evidence against a compromised
  owner needs an external WORM copy (e.g. object storage with object lock) or hash chaining.
  That is listed for the deployment phase.

## D-031 Audit fails closed; query text optional
**Exactly one row per /ask.** The endpoint fills an `AuditRecord`, and the outermost middleware
writes it after the response is built but before it is sent. Requests that never reach the
endpoint are also written, from the status code alone: 401 and 422 become `denied` with a
NULL user, and 5xx and unhandled errors become `error`.
**Fail closed.** If the insert fails, the response is replaced by `503 audit unavailable; request
not served`. The trade-off is availability for accountability: an answer that can't be audited
is exactly what an audit log exists to prevent. `audit_write_failures_total` makes the outage
visible. Tested by unit test (a broken writer gives 503) and by integration tests (success,
denied 401/422, LLM error 502 each write one row).
**Query text.**
- `STORE_QUERY_TEXT=true` (dev default) stores the question.
- `false` stores only `query_hash` = HMAC-SHA256 over the question, normalised for whitespace
  and case. The key is derived from `JWT_SECRET` with a domain-separation label. A plain SHA-256
  of a short question could be reversed by guessing. The consequence: rotating `JWT_SECRET`
  breaks hash correlation with older rows.
- Production with real tenants should run with `false`, because questions themselves can contain
  personal data.

## D-032 GET /admin/audit/documents/{doc_id}
- **Authorization:** admin role (re-read from the DB) or 403.
- **Existence check:** `document_in_current_tenant(doc)`, a `SECURITY DEFINER` boolean function
  that only answers for the caller's own tenant. A foreign doc id and a non-existent one both
  return the same 404 body, so there is no existence leak. The function lets an admin audit a
  confidential document they cannot read; the endpoint returns who, when, query hash and request
  id, never content or query text.
- **Rows:** filtered by RLS and by an explicit `tenant_id` WHERE.
- **Window:** `days` 1–365, default 30.
- **Pagination:** keyset on `(ts DESC, id DESC)` with an opaque cursor (no OFFSET drift).
**Not audited yet:** the admin endpoint's own use. `event_type='acl_change'` is reserved for
Phase 4's permission-change events.

## D-033 Atomic permission changes through SECURITY DEFINER functions (Phase 4)
**Mechanism.** `PATCH /admin/documents/{id}/acl`, `PATCH /admin/users/{id}/roles` and
`PATCH /admin/users/{id}/status` each call one database function (migration 0006). The function
runs as the owner but inside the caller's transaction, and does four things:
1. **Authorizes from the database.** The RLS context must name a user who is, *in the users table
   right now*, an active admin of that tenant. `app.roles` alone is never trusted.
2. **Locks the target row** (`SELECT ... FOR UPDATE`), so concurrent changes to one document
   serialize.
3. **Updates the document AND every chunk of it** (new `acl_version`), or the user row.
4. **Inserts an `acl_change` audit row** with `change_before` / `change_after` JSON.
All four commit together or not at all (tested: invalid changes leave document, chunks and audit
untouched).
**Why functions, not UPDATE grants.** The app role stays SELECT-only on documents, chunks and
users. With UPDATE grants the app could change any ACL column of any row RLS lets it see. With
these functions, the only writes possible are these three validated operations, re-authorized in
the database even if the API layer is bypassed (tested by calling the function directly as a
non-admin, and as an employee whose `app.roles` claims admin).
**Rules.** 404 for another tenant's document or user, identical to one that doesn't exist. `allowed_users` must be users of the same tenant. Public and internal documents can't carry ACL
lists (D-018). An admin can't remove their own admin role or deactivate themselves, so a tenant
can't lose its last way back in by accident.
**Why no outbox or retry queue.** With a separate vector store (Qdrant, Pinecone), an ACL change
means two writes to two systems. Making them consistent needs an outbox table, a relay, retries,
idempotent upserts, and a window in which the vector store still serves the old ACL. That window
is exactly a revocation that hasn't taken effect yet. Here the ACL source of truth and the vector
index live in the **same Postgres**, so the change is one local transaction: there is no window,
no message to lose and no relay to monitor.
**Trade-off.** Vector search is tied to Postgres capacity: HNSW in pgvector, one primary, Neon
compute limits. A dedicated vector database scales search independently and offers features
pgvector lacks. If that move ever happens, this design must gain an outbox plus a query-time
re-check against `documents`. The Phase 3 second check already does the re-check, which is why
it was built that way.
**Measured.** Over 50 revocations, the first query after the PATCH returned was denied 50/50
times. p50 is 751 ms and p95 842 ms from PATCH return to denied query, which is just the length
of one retrieval round trip to Neon (ap-southeast-1). There is no propagation delay.

## D-034 reconcile_acls: consistency checker, fixer and CI gate
- **Why drift can still happen:** after D-033, a chunk can only drift from its document through a
  manual SQL edit or a bug.
- **What `scripts/reconcile_acls.py` does:** it finds chunks whose classification, roles, users
  or `acl_version` differ from their document's. `--fix` copies the document's ACL onto them;
  `documents` is the source of truth, and nothing is ever copied the other way. It exits 1 when
  drift is found.
- **CI gate:** CI runs it after all tests (`--test`).
- **Why this is safe to run late:** the Phase 3 second check already drops a drifted chunk at
  query time, comparing against the authoritative document (fail closed). The fix restores
  availability; it was never the only safety net. The integration test widens one chunk's ACL
  directly: the employee does not get it, reconcile finds exactly that chunk, and the fix makes
  `verify_index` clean again.

## D-035 One retry on a dropped connection (retrieval and audit)
The first full security run hit a Neon pooler blip: several connections failed with
`OperationalError` within 30 ms. Everything failed closed (500/503, no data, answer withheld when
the audit write failed), but users shouldn't see a transient blip.
- **Retrieval** is a read-only transaction, so it is retried once from scratch. A second failure
  returns 503 "Database unavailable".
- **The audit insert** is retried once as well. If the first commit succeeded but its
  acknowledgement was lost, the retry writes a second row for the same request. This is chosen
  deliberately: over-auditing is the safe failure mode, under-auditing is not. A unique index on
  `request_id` was rejected, because a client can reuse an `X-Request-ID`, and "exactly one row"
  must never become "zero rows".

## D-036 Adversarial leakage suite design
- **Independent oracle.** What a persona may see is computed from the manifest and
  `can_access`, never from the API or RLS. Asking the system under test what it may return would
  make the test circular.
- **What counts as a leak.**
  - A retrieved or cited doc id outside the allowed set: exact, id-based.
  - Any forbidden canary, doc id or distinctive title in the body or headers. A string only
    counts if it does **not** occur in the persona's own allowed titles or text. The first run
    produced 19 title false positives (another tenant's "2026 Salary Bands" is a substring of the
    persona's own "FY2026 Salary Bands"). Those were investigated before the rule was narrowed:
    0 of them were forbidden retrievals.
  - Any audit row listing a forbidden chunk.
- **Coverage.**
  - 330 attack queries in 15 families: 195 questions asked by 4 personas each (round-robin, so all
    21 active personas attack), plus 135 crafted HTTP requests (tampered body, query and
    headers; 13 tampered tokens, including all 3 deactivated users; admin probing with
    byte-identical 404s; SQL-injection-style path, query and body parameters).
  - About 900 requests per run, sent concurrently to a real server.
  - **Positive controls** (HR reads salaries; an employee reads their own review and leave
    policy) make sure "0 leaks" can't come from a system that returns nothing.
- **Sampling.** The full cross product (195 × 21) would take about an hour against Neon from this
  laptop. It can be enabled with `SECURITY_PERSONAS_PER_QUESTION=21`.
- **LLM-side limitation.** The fake LLM never obeys injected instructions. A real one might be
  steered by the planted notice into a misleading or garbled answer, but it can't reveal
  forbidden data, because retrieval removed it before the model ran.
- **Placeholder.** A skipped test records that a future semantic cache (Project 1) must be keyed
  by tenant and the caller's ACL fingerprint.

## D-037 Mutation meta-tests: proving the suite can fail
The same 207 (persona, query) pairs are run against deliberately broken systems:

| Mutant | Leaks | Result |
|---|---|---|
| (a) tenant condition removed from WHERE, RLS off, check off | 730 | suite fails |
| (b) owner role (BYPASSRLS), no WHERE, check off | 1,019 | suite fails |
| (c) no `set_config`, no WHERE, check off | 0 | suite fails (positive controls) |
| (d) RLS bound to the wrong tenant's identity, no WHERE, check off | 1,270 | suite fails |

**Why (c) has no leaks.** RLS without a context returns nothing, which is fail closed. The suite
still fails, because the positive controls catch a system that went dark.
**The intact system passes, and so does each layer alone:** WHERE clause only (owner session, no
check), RLS only (no WHERE, no check), post-check only (owner session, no WHERE; forbidden rows
reach layer 3 and are all removed). That is defence in depth demonstrated, not just claimed.

## D-038 CI
`.github/workflows/ci.yml` runs, in order:
1. lint;
2. unit tests (no database);
3. create a throwaway Neon branch `ci-<run>-<attempt>` from `dev`, naming the database and the
   owner role explicitly, because `dev` now has several roles and Neon then returns no default
   connection string;
4. reset the schema, so migrations are proven on an empty database;
5. migrate;
6. integration tests;
7. the security suite and mutation tests;
8. the `reconcile_acls` integrity gate;
9. publish `REPORT.md` to the job summary and upload `report.json` (always);
10. delete the branch (always, even on failure).

The build fails on any leak, failed control, failed mutation test or drift. CI uses the fake LLM
and hash embeddings: no API keys, no cost, deterministic.
**Not verified yet.** This repository has no GitHub remote, so the workflow has not run on
GitHub. Its steps were run end to end, in the same order, on a fresh throwaway Neon branch (see
RESULTS.md). Fork PRs are skipped, because forks can't read secrets.
**Free-tier limits.** Neon has a branch-count limit and compute hours. Each run creates and
deletes one branch, and concurrency cancels superseded runs.
