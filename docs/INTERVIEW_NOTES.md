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
