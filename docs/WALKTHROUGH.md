# Walkthrough: "who retrieved document X?"

Run against a local API (`python -m uvicorn app.main:app --port 8000`) on the seeded dev branch.
The dev password comes from the git-ignored `data/seed_users.json`. It is read into a shell
variable and never typed into a command line or a chat.

```bash
API=http://127.0.0.1:8000
PW=$(python -c "import json;print(json.load(open('data/seed_users.json'))['password'])")
login() { curl -s -X POST $API/auth/login -H 'content-type: application/json' \
  -d "{\"email\":\"$1\",\"password\":\"$PW\"}" | python -c "import json,sys;print(json.load(sys.stdin)['access_token'])"; }
```

## 1. Two employees ask questions

```bash
EMP=$(login employee_1@acme.example)
FIN=$(login finance_1@acme.example)
curl -s -X POST $API/ask -H "Authorization: Bearer $EMP" -H 'content-type: application/json' \
  -d '{"question":"How many PTO days do I get per year?"}' -D - | grep -i -E 'x-request-id|"answer"'
curl -s -X POST $API/ask -H "Authorization: Bearer $FIN" -H 'content-type: application/json' \
  -d '{"question":"How many PTO days do employees get?"}' -o /dev/null -w '%{http_code}\n'
```

Every response carries `X-Request-ID`. The same id is in the server log lines and in the audit row.

## 2. The admin asks "who retrieved acme-leave-policy in the last 30 days?"

```bash
ADM=$(login admin_1@acme.example)
curl -s "$API/admin/audit/documents/acme-leave-policy?days=30&limit=10" \
  -H "Authorization: Bearer $ADM" | python -m json.tool
```

The response lists `user_id`, `ts`, `query_hash` and `request_id` for each retrieval, newest
first, plus `next_cursor` for the next page (`&cursor=<next_cursor>`).

## 3. The boundaries

```bash
# a non-admin gets 403
curl -s -o /dev/null -w '%{http_code}\n' "$API/admin/audit/documents/acme-leave-policy" -H "Authorization: Bearer $EMP"
# another tenant's admin gets 404, identical to a document that does not exist
NWADM=$(login admin_1@northwind.example)
curl -s "$API/admin/audit/documents/acme-leave-policy" -H "Authorization: Bearer $NWADM"
curl -s "$API/admin/audit/documents/does-not-exist" -H "Authorization: Bearer $NWADM"
# client-asserted identity is rejected (422) and logged as suspicious
curl -s -o /dev/null -w '%{http_code}\n' -X POST $API/ask -H "Authorization: Bearer $EMP" \
  -H 'content-type: application/json' -d '{"question":"hi","tenant_id":"northwind"}'
# the security counter
curl -s $API/metrics | grep acl_violations_total
```
