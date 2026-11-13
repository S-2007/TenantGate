# UI test checklist (Phase 6)

Manual walkthrough of the bundled web UI (`/`), run against a local server on the Neon **dev**
branch with `REGISTRATION_ENABLED=true`. Use a company you register yourself: the seed users'
password is not needed for anything below.

Last run: 2026-11-12, Claude's built-in browser, `http://127.0.0.1:8000/`. The test company
(slug `uidemo-…`) was created during the run and its throwaway password was generated locally,
never committed.

| # | Step | Expected | Result |
|---|------|----------|--------|
| 1 | Sign-in page → **Register your company** | Register form shown (or "registration is disabled" when off) | pass |
| 2 | Fill company name, slug, admin email, 12+ char password → **Create company** | Signed in as that admin; tenant pill + `admin` role in the top bar | pass |
| 3 | **Users** tab → add `hr@…` (hr, employee) and `emp@…` (employee) | "Created … They can sign in now."; both rows listed as ACTIVE | pass |
| 4 | Add a user with no role ticked | Inline error "roles: List should have at least 1 item…" | pass |
| 5 | **My documents** → upload `leave_policy.txt` as *internal* | Progress bar, "Received. Processing up-…"; row goes PROCESSING → READY by itself | pass |
| 6 | Upload `salary_bands.txt` as *confidential*, role `hr` | READY; **not** in the admin's "Documents you can read" (admin isn't hr) | pass |
| 7 | Sign in as `emp@…`, ask "What do L4 engineers earn at Orbit Foods?" | "I don't know…"; only the leave policy was searched | pass |
| 8 | Same user: "How many days of paid leave…?" | "27 days" with a citation to leave_policy | pass |
| 9 | Sign in as `hr@…`, salary question | Answer with the band, cited to salary_bands | pass |
| 10 | A second registered company asks about Orbit's salary/leave; GET/DELETE Orbit doc ids | "I don't know", nothing retrieved; 404 for every Orbit doc id; empty document list | pass (API) |
| 11 | Orbit admin → **Replace** leave policy with v2 | Row stays READY, file name becomes `leave_policy_v2.txt` | pass |
| 12 | **Delete** salary bands (confirm dialog) | "Deleted up-… (1 chunks)."; row gone | pass |
| 13 | Upload an `.exe` renamed `payroll.pdf` | "file content is not a PDF" (415), nothing created | pass |
| 14 | Reload the page | Signed out (token is memory-only) | by design |

Not covered by this run (covered by automated tests instead): oversize files (413), zip bombs,
encrypted/oversized PDFs, quota limits, rate limits (429), deactivate/reactivate a user
(`tests/integration/test_uploads.py`, `test_self_service.py`, `tests/unit/test_upload_extract.py`).

Notes:
- The browser pane can't open the OS file picker, so in this run the file inputs were filled
  with a `DataTransfer` object from the page's console; the upload itself went through the
  normal form, XHR and server path.
- The LLM answers came from the dev server's real chain (Groq `openai/gpt-oss-120b`).

## Phase 7 run (2026-11-12, same setup, company "Kestrel Labs" registered in the run)

| # | Step | Expected | Result |
|---|------|----------|--------|
| 15 | Users → Invite a person (maya, hr + employee) | Link shown once (`/#invite=<43-char token>`), invite listed as pending | pass |
| 16 | Open the link in a fresh page load | "You were invited to Kestrel Labs as maya… (employee, hr)"; token removed from the address bar | pass |
| 17 | Choose a password → Create my account | Signed in as maya with employee + hr; no Users / Security / Admin tabs | pass |
| 18 | Use the same link again | 410 "invite is invalid or has expired" | pass |
| 19 | Admin uploads an HR-only file; maya asks twice | Answers with citation ("2.4 million", "every April") | pass |
| 20 | Admin → Security tab | 2 questions, 1 person, 0 refused, 0 blocked; the HR-only doc listed **by id** (admin isn't HR), reader maya ×2; uploads per day; events: registered → invite created → accepted → upload | pass |
| 21 | Account → Sign out of all devices (a second "device" signed in) | Back to sign-in with a message; the other device's token: 200 before, 401 after | pass |

## UI redesign run (2026-11-13, built-in browser, desktop and 375 px mobile, company "Northstar Robotics" registered in the run)

| # | Step | Result |
|---|------|--------|
| 22 | Sign in, register (new auth screens) | pass. Sidebar shows the real company name and slug (`/auth/workspace`), status "API and database healthy · Database round trip 394 ms" from `/health` |
| 23 | Documents → Upload document (modal, drop zone, internal file; confidential HR-only file) | pass. File name and size shown, the ACL box only for confidential, processing → ready by polling, the HR-only file absent from the admin's readable list |
| 24 | People → Invite person (modal) → copy link → Escape closes | pass |
| 25 | Open the link in a fresh page load → preview → set password | pass. Signed in as maya (employee, hr); nav shows only Ask / Documents / Settings |
| 26 | Ask (Ctrl+Enter): HR salary question; unrelated question | pass. Loading skeleton; answer with 1 cited source; refusal state; evidence panel: cited doc, CONFIDENTIAL badge, similarity, request id, model, latency |
| 27 | Admin → Security (7 days) | pass. Real counts, uploads-per-day bars, HR-only doc listed by id only, reader maya ×2, events |
| 28 | People → Edit roles (with an overlapping reload) | pass after the stale-response fix (first run showed old roles) |
| 29 | Audit log → lookup + ACL change | pass. 2 retrievals listed; "confidential [hr] → confidential [admin, hr], 1 chunks updated, version 2" |
| 30 | Replace, delete | pass. Delete OK; the replace row refreshed by itself after the `updated_at` polling fix (first run needed a manual reload) |
| 31 | Settings → change password; sign out of all devices | pass. Other device 200 → 401; back at sign-in with a message |
| 32 | Mobile 375 px: every page | pass. 0 px horizontal overflow on all 6 pages; drawer opens; closes by ×, scrim, Escape and nav; focus returns to the menu button |
| 33 | CSP header after the redesign | unchanged (`default-src 'self'; script-src 'self'; style-src 'self' …`) |
