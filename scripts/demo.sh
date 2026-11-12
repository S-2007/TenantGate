#!/usr/bin/env bash
# Five demo scenarios against a running API (seeded dev branch). Read-only except scenario (e),
# which changes one ACL and ALWAYS restores it (trap), even if the script is interrupted.
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

title "Result"
curl -s "$API/metrics" | grep -E '^acl_violations_total' | sed 's/^/  /'
if [[ $FAILS -eq 0 ]]; then printf '  \033[32mALL 5 SCENARIOS PASSED\033[0m\n'; else printf '  \033[31m%s CHECK(S) FAILED\033[0m\n' "$FAILS"; exit 1; fi
