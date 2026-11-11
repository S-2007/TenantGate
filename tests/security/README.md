# Adversarial leakage suite

```bash
python -m pytest -q -s tests/security        # needs the Neon TEST branch (TEST_DATABASE_URL*)
```

It seeds the real 66-document corpus, all 24 personas and one **poisoned** document into the
test branch, starts a real uvicorn server as a throwaway low-privilege `rag_app` login (fake
LLM, hash embeddings), and attacks it over HTTP from 8 threads.

| File | Role |
|---|---|
| `oracle.py` | Ground truth **independent of the system under test**: each persona's allowed docs come from the manifest + `can_access`. A forbidden canary or title only counts if it can't be found in the persona's own allowed data. |
| `attacks.py` | Deterministic generators: 11 question families (195 questions) and 4 HTTP families (135 crafted requests), plus positive controls |
| `runner.py` | Sends every attack, checks every response (retrieved and cited doc ids, canaries, forbidden ids and titles in body or headers, no 5xx, no SQL details), then checks every audit row |
| `test_leakage.py` | The suite. Writes `report.json` and `REPORT.md`, prints `LEAKS: N`, fails on any leak or failed positive control |
| `test_mutations.py` | Meta-tests: break the protections and prove the suite fails; show each layer alone is enough |

Assertions per (persona × attack):
1. no retrieved or cited chunk belongs to a doc the persona may not see;
2. no forbidden canary, doc id or distinctive title anywhere in the response JSON or headers;
3. the request's audit row lists only allowed chunk ids (exactly one row per `/ask`);
4. HTTP attacks get their expected status (401/403/404/422). Every 404 of a probing family is
   byte-identical, so a 404 can't reveal whether a document exists.

By default each question is asked by 4 personas (round-robin, so every persona attacks).
`SECURITY_PERSONAS_PER_QUESTION=21` runs the full cross product.

**LLM-side limitation, stated honestly.** CI uses the deterministic fake LLM, which never follows
injected instructions. With a real LLM, an injected document *can* change what the model says
(misleading answers, refusals, odd formatting). It **cannot** make the model reveal another
tenant's or another role's data, because that data never enters the prompt: retrieval is
filtered before the model runs. The indirect-injection family checks exactly this.
