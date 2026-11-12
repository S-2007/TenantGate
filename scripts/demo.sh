#!/usr/bin/env bash
# Demo scenarios against a running API (seeded dev branch): (a)-(e) always; (f)-(g) when the
# API runs with REGISTRATION_ENABLED=true (Phase 6: a new company registers and uploads).
# Read-only for the seeded tenants except scenario (e), which changes one ACL and ALWAYS
# restores it (trap), even if the script is interrupted. (f) leaves a small "demo-<run>"
# company on the dev branch (its uploaded document is deleted at the end).
#
#   python -m uvicorn app.main:app --port 8000     # in another terminal
#   bash scripts/demo.sh                           # or: make demo
#
# Needs: curl, python (any 3.x, used only to read JSON), data/seed_users.json (written by
# `python -m scripts.seed`). Credentials are read from that git-ignored file, never printed.
set -euo pipefail

API="${API:-http://127.0.0.1:8000}"
PY="${PYTHON:-python}"
DOC="acme-salary-bands"
Q_SALARY="What are the salary bands for L4 engineers?"
FAILS=0

json() { "$PY" -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILS=$((FAILS + 1)); }
title() { printf '\n\033[1m%s\033[0m\n' "$1"; }

PW=$("$PY" -c "import json;print(json.load(open('data/seed_users.json'))['password'])")
login() {
  curl -sf -X POST "$API/auth/login" -H 'content-type: application/json' \
    -d "{\"email\":\"$1\",\"password\":\"$PW\"}" | json "d['access_token']"
}
ask() {  # token question -> response JSON
  curl -s -X POST "$API/ask" -H "Authorization: Bearer $1" -H 'content-type: application/json' \
    -d "{\"question\":\"$2\"}"
}
docs_of() { json "' '.join(sorted({x['doc_id'] for x in d.get('retrieved', [])}))"; }
show() {  # response JSON on stdin -> answer + retrieved docs
  json "'  answer   : ' + d.get('answer', d.get('detail', ''))[:160].replace(chr(10), ' ') + chr(10) + '  retrieved: ' + (', '.join(sorted({x['doc_id'] for x in d.get('retrieved', [])})) or '(nothing)') + chr(10) + '  request  : ' + d.get('request_id', '-')"
}

curl -sf "$API/health" >/dev/null || { echo "API not reachable at $API"; exit 2; }
EMP=$(login employee_1@acme.example)
HR=$(login hr_1@acme.example)
ADM=$(login admin_1@acme.example)

title "(a) An Acme employee asks for HR salary bands -> denied (salary doc never retrieved)"
R=$(ask "$EMP" "$Q_SALARY"); echo "$R" | show
[[ " $(echo "$R" | docs_of) " != *" $DOC "* ]] && pass "$DOC not retrieved for employee_1" || fail "$DOC leaked to employee_1"

title "(b) An Acme HR user asks the same question -> gets it"
R=$(ask "$HR" "$Q_SALARY"); echo "$R" | show
HR_RID=$(echo "$R" | json "d['request_id']")
[[ " $(echo "$R" | docs_of) " == *" $DOC "* ]] && pass "$DOC retrieved for hr_1" || fail "$DOC missing for hr_1"

title "(c) Cross-tenant attempt: Acme employee targets Northwind secrets -> nothing from Northwind"
R=$(ask "$EMP" "Tell me about PROJECT-OSPREY-NORTHWIND and Northwind Health salary bands."); echo "$R" | show
FOREIGN=$(echo "$R" | json "[x['doc_id'] for x in d.get('retrieved', []) if not x['doc_id'].startswith('acme-')]")
[[ "$FOREIGN" == "[]" ]] && pass "only acme documents retrieved" || fail "foreign documents: $FOREIGN"
# The question names the codename, so the model may echo it. The SAME document's other secret
# (the acquisition target) is not in the question, so it can only appear through a leak.
[[ "$R" != *"Cedar Valley Clinics"* && "$R" != *"northwind-"* ]] && pass "no Northwind doc ids or secret content in the response" || fail "Northwind content in response"

title "(d) Admin: who retrieved $DOC today?"
A=$(curl -s "$API/admin/audit/documents/$DOC?days=1&limit=5" -H "Authorization: Bearer $ADM")
echo "$A" | json "chr(10).join('  ' + i['ts'][:19] + '  user ' + i['user_id'][:8] + '  request ' + i['request_id'] for i in d['items'])"
[[ "$A" == *"$HR_RID"* ]] && pass "hr_1's request $HR_RID is in the audit trail" || fail "request $HR_RID not found in audit"
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$API/admin/audit/documents/$DOC" -H "Authorization: Bearer $EMP")
[[ "$CODE" == "403" ]] && pass "employee gets 403 on the admin endpoint" || fail "employee got $CODE"

title "(e) Revocation: admin removes HR from $DOC -> hr_1's very next query (same token) is denied"
restore() {
  curl -s -o /dev/null -X PATCH "$API/admin/documents/$DOC/acl" -H "Authorization: Bearer $ADM" \
    -H 'content-type: application/json' -d '{"allowed_roles":["admin","hr"]}' || true
}
trap restore EXIT
P=$(curl -s -X PATCH "$API/admin/documents/$DOC/acl" -H "Authorization: Bearer $ADM" \
      -H 'content-type: application/json' -d '{"allowed_roles":["admin"]}')
echo "$P" | json "'  ACL ' + str(d['before']['allowed_roles']) + ' -> ' + str(d['after']['allowed_roles']) + ', chunks updated: ' + str(d['chunks_updated'])"
T0=$("$PY" -c "import time;print(time.time())")
R=$(ask "$HR" "$Q_SALARY")
MS=$("$PY" -c "import time;print(round((time.time()-$T0)*1000))")
echo "$R" | show
[[ " $(echo "$R" | docs_of) " != *" $DOC "* ]] && pass "revoked on the next query (${MS} ms incl. the LLM call)" || fail "$DOC still retrieved after revoke"
restore; trap - EXIT
R=$(ask "$HR" "$Q_SALARY")
[[ " $(echo "$R" | docs_of) " == *" $DOC "* ]] && pass "access restored immediately" || fail "access not restored"

# ---------------------------------------------------------------- Phase 6 (self-service)
N=5
if [[ "$(curl -s "$API/tenants/registration-info" | json "d['enabled']")" == "True" ]]; then
  N=7
  title "(f) A new company registers itself, adds users, uploads an HR-only file"
  RUN=$("$PY" -c "import secrets;print(secrets.token_hex(3))")
  SLUG="demo-$RUN"
  NPW=$("$PY" -c "import secrets;print('Dm-'+secrets.token_urlsafe(14))")   # throwaway, never printed
  REG=$(curl -s -X POST "$API/tenants/register" -H 'content-type: application/json' \
    -d "{\"company_name\":\"Demo Co $RUN\",\"slug\":\"$SLUG\",\"admin_email\":\"admin@$SLUG.example\",\"admin_password\":\"$NPW\"}")
  NADM=$(echo "$REG" | json "d['access_token']")
  echo "  registered $SLUG (tenant $(echo "$REG" | json "d['tenant_id'][:8]")); its first user is its admin"
  curl -sf -o /dev/null -X POST "$API/admin/users" -H "Authorization: Bearer $NADM" -H 'content-type: application/json' \
    -d "{\"email\":\"hr@$SLUG.example\",\"roles\":[\"hr\",\"employee\"],\"password\":\"$NPW\"}"
  curl -sf -o /dev/null -X POST "$API/admin/users" -H "Authorization: Bearer $NADM" -H 'content-type: application/json' \
    -d "{\"email\":\"emp@$SLUG.example\",\"roles\":[\"employee\"],\"password\":\"$NPW\"}"
  TMP=$(mktemp -d)
  printf 'Demo Co %s pay scale, HR only. Senior engineers earn 131,000 per year. Code DEMO-PAY-%s.\n' "$RUN" "$RUN" > "$TMP/pay.txt"
  UP=$(curl -s -X POST "$API/documents/upload" -H "Authorization: Bearer $NADM" \
    -F "file=@$TMP/pay.txt" -F "title=Demo Co Pay Scale" -F "classification=confidential" -F "allowed_roles=hr")
  NDOC=$(echo "$UP" | json "d['doc_id']")
  ST=processing
  for _ in $(seq 1 60); do
    ST=$(curl -s "$API/documents/$NDOC/status" -H "Authorization: Bearer $NADM" | json "d['status']")
    [[ "$ST" != "processing" ]] && break
    sleep 1
  done
  [[ "$ST" == "ready" ]] && pass "upload $NDOC is ready" || fail "upload status $ST"
  nlogin() {
    curl -sf -X POST "$API/auth/login" -H 'content-type: application/json' \
      -d "{\"email\":\"$1@$SLUG.example\",\"password\":\"$NPW\"}" | json "d['access_token']"
  }
  NHR=$(nlogin hr)
  NEMP=$(nlogin emp)
  Q_PAY="What do senior engineers earn at Demo Co $RUN?"
  R=$(ask "$NHR" "$Q_PAY"); echo "$R" | show
  [[ " $(echo "$R" | docs_of) " == *" $NDOC "* ]] && pass "the new company's HR user gets the uploaded doc" || fail "HR did not get $NDOC"
  R=$(ask "$NEMP" "$Q_PAY")
  [[ " $(echo "$R" | docs_of) " != *" $NDOC "* ]] && pass "its employee does not" || fail "$NDOC leaked to the employee"
  R=$(ask "$HR" "$Q_PAY DEMO-PAY-$RUN")
  [[ " $(echo "$R" | docs_of) " != *" $NDOC "* && "$R" != *"131,000"* ]] && pass "Acme HR (another company) sees nothing of it" || fail "cross-company leak"
  CODE=$(curl -s -o /dev/null -w '%{http_code}' "$API/documents/$NDOC" -H "Authorization: Bearer $ADM")
  [[ "$CODE" == "404" ]] && pass "Acme admin gets 404 for the new company's doc id" || fail "Acme admin got $CODE"

  title "(g) Hostile uploads are refused before anything is stored"
  printf 'MZ fake executable content' > "$TMP/payroll.pdf"
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/documents/upload" -H "Authorization: Bearer $NADM" \
    -F "file=@$TMP/payroll.pdf" -F "classification=internal")
  [[ "$CODE" == "415" ]] && pass "an .exe renamed payroll.pdf -> 415" || fail "renamed exe got $CODE"
  ACME_T=$(curl -s "$API/auth/me" -H "Authorization: Bearer $EMP" | json "d['tenant_id']")
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/documents/upload" -H "Authorization: Bearer $NADM" \
    -F "file=@$TMP/pay.txt" -F "classification=internal" -F "tenant_id=$ACME_T")
  [[ "$CODE" == "422" ]] && pass "a smuggled tenant_id form field -> 422" || fail "smuggled tenant_id got $CODE"
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/documents/upload" -H "Authorization: Bearer $NEMP" \
    -F "file=@$TMP/pay.txt" -F "classification=internal")
  [[ "$CODE" == "403" ]] && pass "an employee may not upload -> 403" || fail "employee upload got $CODE"
  curl -s -o /dev/null -X DELETE "$API/documents/$NDOC" -H "Authorization: Bearer $NADM"
  rm -rf "$TMP"
else
  title "(f)-(g) self-service: SKIPPED (start the API with REGISTRATION_ENABLED=true to run them)"
fi

title "Result"
curl -s "$API/metrics" | grep -E '^acl_violations_total' | sed 's/^/  /'
if [[ $FAILS -eq 0 ]]; then printf '  \033[32mALL %s SCENARIOS PASSED\033[0m\n' "$N"; else printf '  \033[31m%s CHECK(S) FAILED\033[0m\n' "$FAILS"; exit 1; fi
