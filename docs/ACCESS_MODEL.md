# Access model

One rule decides whether a user may read a document: `can_access(user, doc_acl)` in
[`app/auth/policy.py`](../app/auth/policy.py). It is a pure function with no I/O. The post-retrieval
check (Phase 3), the eval review and the tests all call this same function. Anything it does not
explicitly allow is denied.

## Inputs

| Input | Where it comes from |
|---|---|
| `user.id`, `user.tenant_id`, `user.roles`, `user.is_active` | The `users` row, re-read from Postgres on **every request** after the JWT is verified. Token claims are never used for authorization. |
| `doc.tenant_id`, `doc.classification`, `doc.allowed_roles`, `doc.allowed_users` | The `documents` row (source of truth). Each chunk carries a copy, written by ingestion and audited by `scripts/verify_index.py`. |

Roles: `employee`, `manager`, `hr`, `finance`, `engineering`, `admin` are **employee roles**.
`contractor` is a known role that is **not** an employee role. Unknown role strings grant nothing.

## Decision table

Rows are checked top to bottom. The first row that matches decides.

| # | Condition | Decision |
|---|---|---|
| 1 | user or document missing | **DENY** |
| 2 | user is not active (`is_active` is anything but `true`) | **DENY** |
| 3 | user tenant ≠ document tenant, or either id is malformed | **DENY**: this also applies to `admin`, which is tenant-scoped |
| 4 | `public` | **ALLOW**: any authenticated, active user of the same tenant. No anonymous or cross-tenant sharing. |
| 5 | `internal` and the user holds ≥ 1 employee role | **ALLOW** |
| 6 | `internal` otherwise (e.g. only `contractor`) | **DENY** |
| 7 | `confidential` and (user roles ∩ `allowed_roles` ≠ ∅ **or** user id ∈ `allowed_users`) | **ALLOW** |
| 8 | `confidential` otherwise | **DENY** |
| 9 | any other classification value | **DENY** |

### Consequences

- **Admin has no implicit content access.** An admin reads a confidential document only if `admin`
  is in its `allowed_roles`. Admins manage users and ACLs, not content (least privilege). In the
  corpus, `admin` is listed on every confidential role-based document, but not on the three
  per-user performance reviews.
- **User-specific documents** (`allowed_users`) are readable only by that user id, and only inside
  its own tenant. The same UUID in another tenant is still denied, by row 3.
- **ACL lists belong only to confidential documents.** For `public` and `internal` documents, both
  lists must be empty. This is enforced by `CHECK` constraints on `documents` and `chunks`, by
  `ChunkPayload`, and by the manifest validator. A role list on an internal document would *look*
  like a restriction while the rule above ignores it, so it is rejected. A restricted internal
  document must be classified `confidential` instead.
- **A confidential document must name someone.** Empty `allowed_roles` *and* empty
  `allowed_users` is rejected by the database, so a document nobody can read can't exist.

## Where each rule is enforced

| Layer | What it guarantees | Phase |
|---|---|---|
| `can_access` (Python) | The decision table above | 2 |
| DB constraints on `documents` / `chunks` | Every chunk has a tenant and a document of the **same** tenant (composite FK). ACL shape rules hold. Known roles only. | 2 |
| `ChunkPayload` + ingestion | Chunk ACL = document ACL at write time. Failures abort the run. | 2 |
| `scripts/verify_index.py` | Audits the whole table for drift (tenant, ACL values, `acl_version`, cross-tenant user grants) | 2 |
| `rag_app` database role | The running app can only `SELECT`. No DDL, no writes, no `BYPASSRLS`. | 2 |
| ACL pre-filter in the vector query, Postgres RLS, post-retrieval `can_access` | Retrieval returns only allowed chunks | 3 |
| `admin_set_document_acl` / `admin_set_user` (SECURITY DEFINER, one transaction) | ACL, role and activation changes apply to the document, all its chunks and the audit log at once, so the next query sees them; only an active admin of the same tenant (checked in the DB) can make them | 4 |
| `scripts/reconcile_acls.py` + `tests/security` in CI | Drift is detected and repaired; 330 attacks must produce zero leaks on every build | 4 |

## Seeded personas (dev and test only)

Each tenant (`acme`, `northwind`, `lumen`) has 8 users: `employee_1`, `employee_2` (**deactivated**),
`manager_1`, `hr_1`, `finance_1`, `engineering_1`, `admin_1`, and `multi_1` (`manager` + `finance`).
Emails are `<slot>@<tenant>.example` (a reserved TLD). The dev password lives in
`data/seed_users.json`, which is git-ignored. `scripts/seed.py` refuses to run with
`ENV=production` or against the Neon default branch.

## Changing permissions (Phase 4)

| Endpoint | Who | Effect |
|---|---|---|
| `PATCH /admin/documents/{doc_id}/acl` `{classification?, allowed_roles?, allowed_users?}` | admin of the same tenant | document + every chunk + `acl_change` audit row, atomically; `acl_version` +1 |
| `PATCH /admin/users/{user_id}/roles` `{roles}` | admin of the same tenant | roles are re-read on every request, so this applies to the user's next request with the same token |
| `PATCH /admin/users/{user_id}/status` `{is_active}` | admin of the same tenant | a deactivated user's still-valid token fails on its next use |

Another tenant's document or user gets 404, identical to one that does not exist. An admin
cannot remove their own admin role or deactivate themselves.


## Phase 6 additions

**Uploaded documents follow the same rules.** Their ACL is set at upload time:
- the classification;
- for confidential documents, roles and/or named users, who must be active users of the
  *same* company.

It can be changed later with the existing ACL PATCH.

**Only `ready` documents exist for retrieval.** Documents still processing, or that failed, are
invisible to everyone in all three layers.

**Uploader metadata (D-045).** The uploader may see the metadata (title, status, size, chunk
count, last error) of their own uploads at `GET /documents/mine` and `GET /documents/{id}`,
even when the ACL excludes them. Content is never shown this way.

**Who may manage a document.**
- Upload: roles in `UPLOAD_ROLES` (default `admin`).
- Replace or delete: the uploader, or an admin of the same company.
- Everyone else, and every other company, gets 404.
