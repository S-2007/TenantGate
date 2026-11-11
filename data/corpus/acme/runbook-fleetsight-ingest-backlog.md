# RUNBOOK: FleetSight telemetry ingest backlog

**Service:** fleetsight-ingest (Kafka consumers on EKS, AWS us-east-2)
**Owners:** FleetSight Platform team - #fleetsight-platform
**Last reviewed:** 17-Jul-2026 by Arjun Mehta
**Audience:** engineering on-call, platform admins

---

## Background

Connected robots publish telemetry to the FleetSight edge gateway over MQTT. The gateway writes messages to the Kafka topic `telemetry.raw.v3` (48 partitions). The `ingest-worker` consumer group reads that topic, normalizes the payloads, and writes to TimescaleDB (hot data, 30 days) and S3 (cold archive). Dashboards and alerting read from TimescaleDB.

When ingest-worker falls behind, customers see stale dashboards and, worse, maintenance alerts (overtemp, gearbox vibration) fire late or not at all.

## Alerting

The Datadog monitor `fleetsight-ingest-consumer-lag` pages the FleetSight on-call when total consumer lag on `telemetry.raw.v3` exceeds 500,000 messages for 10 consecutive minutes. A sustained lag above 500,000 messages is treated as a SEV2 incident; if lag exceeds 5,000,000 messages or alerting is fully stopped for all customers, escalate to SEV1.

Lag between 100k and 500k usually clears on its own within a few minutes after a deploy. Watch it, don't page anyone.

## Quick triage (first 10 minutes)

1. Open the **FleetSight / Ingest** dashboard in Datadog. Check:
   - consumer lag per partition (is it all partitions or a few?)
   - ingest-worker pod count and restarts
   - TimescaleDB write latency (p99)
2. Check #deploys - was anything shipped to ingest-worker, the gateway, or the DB in the last hour?
3. Run:

```
kubectl -n fleetsight get pods -l app=ingest-worker
kubectl -n fleetsight logs -l app=ingest-worker --since=15m | grep -i -E "error|timeout" | tail -50
```

## Common causes and fixes

### A. Lag on all partitions, DB write latency high

TimescaleDB is the bottleneck. Usually a compression job or a runaway dashboard query.

- Check `pg_stat_activity` for long-running queries over 60 seconds. Cancel any ad-hoc analytics queries (they should be on the read replica anyway).
- If a compression job is running during business hours, pause it: `SELECT alter_job(<job_id>, scheduled => false);` and re-enable after lag clears.
- Do NOT scale ingest-worker up in this case - more workers will just pile more writes on a struggling database.

### B. Lag on a few partitions only

A "hot" partition, usually one large customer with a misconfigured robot sending telemetry at 100 Hz instead of 1 Hz.

- Find the top publishers: run the `top-publishers` saved query in Datadog Logs.
- Apply a per-device rate limit at the gateway: `fsctl gateway ratelimit --device <serial> --hz 1`
- Tell Customer Support so they can contact the customer.

### C. Pods crash-looping after a deploy

- Roll back: `argocd app rollback fleetsight-ingest <previous-revision>`
- Post in #deploys that you rolled back and why.

### D. Kafka broker issues

If brokers show under-replicated partitions, this is an MSK issue. Open a high-priority AWS support case and loop in the platform admin on call.

## Scaling

ingest-worker is capped at 48 replicas (one per partition). Scaling beyond 48 does nothing. If you are already at 48 and the DB is healthy, the fix is a code or partitioning change, not more pods.

```
kubectl -n fleetsight scale deploy/ingest-worker --replicas=48
```

## After recovery

- Confirm lag is below 50,000 and falling.
- Check that delayed maintenance alerts were delivered (the alert-replayer job runs automatically every 5 min).
- If this was a SEV2, start the postmortem doc from the template - the clock is ticking.
