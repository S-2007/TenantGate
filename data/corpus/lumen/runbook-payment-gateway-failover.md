# RUNBOOK: Payment Gateway Failover (Lumen Pay authorizations)

Service owner: Payments Platform team · On-call: PagerDuty "payments-primary"
Last tested: 2026-07-16 (Q3 game day) · Last edited: 2026-07-18 by Diego Ferreira

> Audience: engineering on-call and admins. Do not run this without being on an incident call.

## Background

Lumen Pay sends card authorizations through **Gatekeeper** (our API gateway, running on EKS in `us-east-2`) to one of two upstream processors:

- **Primary:** Corvane Processing (≈85% of traffic in normal operation)
- **Secondary:** Tessaline Payments (≈15% of traffic, kept warm so failover is not a cold start)

Routing weights live in the `processor-routing` config in AWS AppConfig. The `auth-router` service reads it every 10 seconds. Normal setting is `corvane: 85, tessaline: 15`.

## When to use this runbook

Use it when the **primary processor** is failing and the incident is a SEV1 or SEV2. Typical signals:

* Datadog monitor `lumenpay.auth.error_rate.corvane` above 5% for 3 consecutive minutes
* Corvane timeouts (p99 latency > 4s) in the `auth-router` dashboard
* Corvane posts an incident on their status page or calls our TAM line

Do NOT fail over for errors that are clearly on our side (e.g. Gatekeeper 5xx across both processors, or Beacon timeouts). Failing over won't help and adds noise.

## Pre-checks (2 minutes, max)

1. Confirm the page is acknowledged — SEV1 ack target is 5 minutes.
2. Check the `auth-router` dashboard: is the error rate elevated **only** for Corvane? If Tessaline is also unhealthy, stop and escalate to the Payments EM.
3. Check Tessaline headroom: our contracted ceiling with Tessaline is **1,400 TPS**. Normal peak total traffic is ~950 TPS, so a full failover fits, but during November peak it may not.
4. Post in the incident channel: "Starting gateway failover per runbook, step 1."

## Procedure

### Step 1 — Shift traffic

```bash
lumenctl routing set --profile prod --corvane 0 --tessaline 100 \
  --reason "INC-<ticket>: Corvane degraded"
```

`lumenctl` requires the `payments-oncall` Okta group and a hardware key touch. The change is written to AppConfig and takes effect in ≤10 seconds.

### Step 2 — Verify

- Watch `lumenpay.auth.success_rate` — should recover above 98% within 2 minutes.
- Check that the Tessaline 3DS flow is working: run the synthetic test `synthetics/auth-3ds-tessaline` from the Datadog UI.
- Spot-check Lumen Hub for a test merchant (`MID 900001 - Lumen QA Coffee`).

### Step 3 — Handle in-flight transactions

Authorizations that timed out at Corvane may have been approved on their side. The `auth-reconciler` job runs every 15 minutes and voids orphaned Corvane approvals automatically. Do **not** void manually unless the job is failing.

### Step 4 — Communicate

Tell the Comms Lead the failover is complete. Status page wording: "Card payments are processing normally. Some transactions between HH:MM and HH:MM may have been declined and can be retried."

## Failing back

Only fail back after Corvane has been healthy for **30 minutes** and their incident is marked resolved.

1. Ramp in stages: `--corvane 25 --tessaline 75`, wait 10 min; `--corvane 60 --tessaline 40`, wait 10 min; then `--corvane 85 --tessaline 15`.
2. Watch error rates at each stage. Any error spike above 2%: go back to the previous stage.
3. Note each step with timestamps in the incident channel for the scribe.

## Known gotchas

- Tessaline does not support **Amex** for merchants onboarded before 2024. Those transactions will decline during failover; this is expected and should be in the merchant comms.
- If AppConfig itself is down, use the break-glass static override: set env var `ROUTING_OVERRIDE=tessaline` on the `auth-router` deployment (`kubectl -n payments set env deploy/auth-router ...`). Remove it afterwards or the next deploy will keep it.
- Fees at Tessaline are about 3 basis points higher. Finance (Treasury) wants a heads-up if we're on Tessaline for more than 6 hours.

## Contacts

| Who | How |
|---|---|
| Payments EM on duty | PagerDuty "payments-em" |
| Corvane TAM line | in 1Password vault "Payments-Vendors" |
| Tessaline support | in 1Password vault "Payments-Vendors" |
| Treasury on-call | PagerDuty "finance-treasury" |
