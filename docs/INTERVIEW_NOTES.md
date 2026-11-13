# Interview notes

Short answers, each backed by something in this repo. The numbers are in `docs/RESULTS.md`.

**1. Pre-filtering or post-filtering, and why?**
Pre-filtering. With post-filtering (retrieve the top-k, then drop forbidden rows), a user whose
nearest neighbours are all forbidden gets nothing, and the forbidden text has already left the
database. In the test where the unfiltered top-10 were all HR-only chunks, post-filtering
returned 0 results and pre-filtering returned the best 5 allowed chunks. Over the eval
questions, post-filtering (fetch 10, keep 5) came up short of k for the share of queries shown
in RESULTS. Pre-filtering also keeps forbidden text off the wire entirely.

**2. How do HNSW indexes and filters interact?**
HNSW explores `ef_search` candidates, and the WHERE clause runs on those. A selective filter can
therefore return fewer than k rows. pgvector 0.8 adds `hnsw.iterative_scan`: with
`strict_order` the scan keeps going until it has k permitted rows (or `max_scan_tuples`), and the
order stays exact. The test forces the HNSW plan, verified with EXPLAIN. With iterative scan off, a
contractor who can read 8 of 256 chunks got fewer than 5 results; with it on, exactly 5. RESULTS
also reports recall@5 with the filter at about 20k chunks.

**3. Why RLS when the WHERE clause already filters?**
Defense in depth against our own bugs. RLS is enforced by Postgres for every query the app role
runs, including one someone writes next year without the clause. It is FORCE-d, bound per
transaction with `set_config(..., true)` (safe with Neon's transaction-mode pooler), and fails
closed when no context is set. The mutation tests show each layer alone is enough, and the
equivalence test shows WHERE == RLS == `can_access` on every (user, chunk) pair.

**4. Why does identity come only from the token?**
Anything in the body, query string or headers is attacker-controlled. The verified JWT gives the
user id; the server then reloads that user from Postgres on every request (active? same tenant?
current roles?). A `tenant_id` in the body is a 422 plus a security log entry. Re-reading
roles means a demotion or deactivation applies on the next request, not when the token expires.

**5. How does revocation work, and why is it atomic?**
A database function changes the document's ACL and every chunk copy, and writes the audit row,
in one transaction. The next query reads the committed rows. Over 50 revocations, the first query
after the PATCH returned was denied every time. The p95 time is about one retrieval round trip,
so there is zero propagation delay. With a separate vector database you'd need an outbox, a relay,
retries, and a window during which revoked chunks are still served.

**6. What does the second check protect against?**
Bugs in layers 1 and 2: a dropped WHERE term, an RLS policy edited by mistake, a chunk ACL
copy that drifted, or a stale `acl_version`. It re-runs `can_access` on the chunk and on the
authoritative document in the same transaction. Only its output type, `AuthorizedChunk`, can
reach the prompt builder. In fault injection (WHERE removed and RLS bypassed), forbidden chunks
reached it and it dropped all of them. The fake LLM's prompt contained none, and the alert
counter went up. In normal operation it dropped 0.

**7. Why is the audit log append-only, and why fail closed?**
An audit trail the app can edit proves nothing. The app role has INSERT/SELECT only, triggers
block UPDATE, DELETE and TRUNCATE even for the owner, and RLS scopes the rows to a tenant. Every
/ask writes exactly one row, including 401s and 422s. If the write fails, the answer is
withheld with a 503: an unaudited answer is exactly what the log exists to prevent.

**8. How did you test for leaks?**
An adversarial suite drives a real server over HTTP: 330 attacks in 15 families, roughly 900
requests from all 24 personas, with CI running it on every push. The oracle is independent of
the system under test (the manifest plus `can_access`). Per response it checks that no forbidden
doc id is retrieved or cited, that no forbidden canary, id or title appears in the body or
headers, that the audit row lists only allowed chunks, and that the expected status code comes
back with identical 404 bodies. Positive controls make sure "0 leaks" can't come from a system
that returns nothing.

**9. What do the mutation tests prove?**
That the suite can fail. Removing the protections gave 730, 1,019 or 1,270 leaks, so the suite
fails. Skipping `set_config` failed closed (0 leaks), but the suite still fails because the
positive controls break. Each single layer alone gives 0 leaks. A leak suite that can't fail
would be worthless.

**10. Neon specifics: pooling and cold starts?**
The app uses the pooled (PgBouncer transaction-mode) host, so prepared statements are off and
settings are transaction-local. Migrations and admin jobs use the direct host. Connects retry
with exponential backoff. Cold start after a suspend, pooled versus direct latency, and retry
counts are measured in RESULTS. A pooler blip during testing was absorbed by one read-only
retry; the audit insert is retried too, and over-audits rather than under-audits.

**11. How would this scale beyond one Postgres?**
First: a bigger Neon compute, read replicas for retrieval, partitioning chunks by tenant, and
tuning `ef_search` and `m`. RESULTS has numbers for about 20k chunks. Beyond that: a dedicated
vector store with tenant-scoped namespaces plus an outbox for ACL changes, keeping the
post-retrieval check against Postgres as the source of truth. That check is what makes
eventual consistency in the vector store safe.

**12. What would you do differently?**
- Use RS256 with key rotation from the start.
- Cut round trips per request: the auth lookup, RLS context and search could share one
  transaction, which matters with a 125–400 ms RTT to Neon.
- Run eval comparisons on a single LLM. Free-tier limits forced a model mix, which RESULTS
  discloses.
- Add a semantic cache keyed by tenant plus ACL fingerprint.
- Keep an external WORM copy of the audit log.

## Phase 6: self-service tenants and uploads

**13. Anyone can now create a company. Why doesn't that break tenant isolation?**
Isolation never depended on tenants being few or trusted. It depends on the tenant id coming
from the verified identity, never from the request.
- The new tenant's id, its admin's user id and every document id are generated by the
  database.
- Every write runs in a SECURITY DEFINER function that reads the tenant from the transaction
  identity (`app_tenant_id()`), checks the caller against the `users` table, and audits in the
  same transaction.
- The leak suite registers three companies at test time and runs the whole attack catalog
  across them and the seeded ones, with LEAKS: 0. Mutation (i) makes the handler trust a form
  `tenant_id`, and the suite fails. The database refused the forged identity on its own.

**14. How do you make an upload safe when the file is hostile?**
- Size is checked as bytes stream in, not from Content-Length.
- The type comes from magic bytes, which must agree with the extension and an allow-list.
- A .docx zip is inspected before inflating: entry count, declared size, per-entry ratio,
  encryption, `..` paths, macros. Then only `document.xml` is inflated, with a hard cap.
- PDFs: encrypted ones are rejected, and pages and characters are capped.
- The extracted text is stripped of control and bidi characters, and the file name is reduced
  to a safe base name.
- What I'd add for production: AV scanning and parsing in a sandboxed worker without network
  access.

**15. Why can't a bug in the upload code give a chunk the wrong ACL?**
The Python code sends only text and vectors. `upload_document_complete` copies classification,
allowed roles and users, tenant and `acl_version` from the documents row inside the database.
The chunks and the `ready` flip commit together. On top of that:
- the composite FK forces `chunk.tenant_id = document.tenant_id`;
- the post-retrieval check compares the chunk's copy with the document;
- `verify_index` audits the whole index in CI.

**16. A document is half-processed. What stops it from being retrieved?**
Three independent layers check `status = 'ready'`: an EXISTS in the vector query's WHERE, the
same EXISTS in the RLS policy, and the post-retrieval check (`document_not_ready`, counted as a
security alert). In normal operation no chunk of a non-ready document exists, because chunks
are inserted in the transaction that flips to ready. The filter is there for the case where
that invariant breaks. Mutation (iii) turns the filter off in all three layers and a planted
half-written document becomes retrievable, so the suite fails. Turning it off in any two
layers still hides it. `reconcile_acls --fix` deletes such chunks.

**17. What breaks first if this gets real traffic?**
- **Background ingestion.** It uses FastAPI BackgroundTasks, so a crash leaves a document in
  `processing`. It is never retrievable, but it is stuck until someone deletes and re-uploads
  it. Next step: a Postgres job table with `SKIP LOCKED` and retries.
- **CPU embedding.** It competes with request handling, so a separate worker is needed.
- **Rate limits.** They are per process. A shared limiter is needed.
- **Quotas.** They are counts. Storage-byte quotas and per-tenant cost accounting are next.

RESULTS.md has the measured upload-to-ready latency and the 10-tenant benchmark.

## Phase 7: accounts

**18. Why invites instead of letting the admin set a password?**
If the admin sets the password, the admin knows it, and it travels through chat. With an invite,
the person chooses their own password.
- The link carries a 256-bit token in the URL *fragment*, which browsers never send to servers
  or in Referer headers. The database stores only its SHA-256.
- The link is single-use, expires and can be revoked.
- Every unusable state returns the same 410.
- Mutation M3 lets a used link be accepted again, and the suite fails.

**19. How do you log someone out everywhere with stateless JWTs?**
- I don't keep a deny-list. Each user has a `token_version`, copied into the token as `tv`.
- Every request already re-reads the user row for roles, so comparing the version costs
  nothing.
- Logout-everywhere, a password change or an admin's revoke bump the version, and every older
  token dies on its next use. Mutation M1 removes the comparison and the suite fails.

**20. Tell me about a concurrency bug you found.**
- **The bug.** Two admins could demote each other at the same moment and leave the company with
  zero admins. Each request passed its own check, because neither saw the other's uncommitted
  change.
- **How I proved it.** I reproduced it deterministically with two real transactions: the result
  was `['ok', 'ok']` and 0 admins.
- **The fix.** A per-tenant advisory lock, plus a "keep one active admin" check that runs after
  the lock, so it sees the committed state. The same test now leaves exactly one admin, and the
  mutation test proves the suite catches the old version.

**21. An admin dashboard over the audit log: what can go wrong?**
- **Cross-tenant aggregation.** It's an aggregate query, so forgetting the tenant filter leaks
  other companies' data in totals and top-N lists. The aggregates run in one definer function
  that reads only the bound tenant.
- **Titles.** It can reveal titles of confidential documents the admin may not read. Those
  appear by id only.
- **Tests.** The suite checks every admin listing for other companies' slugs and ids. Mutation
  M4 drops the tenant filter and the suite fails.
