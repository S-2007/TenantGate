# Incident Response Process

Owner: Site Reliability & Security (Omar Haddad, CISO; Priscilla Tan, VP Engineering)
Last updated: 2026-05-28 (rev 4, after the April security incident review)

This document describes how Acme Robotics detects, escalates, manages and closes incidents that affect customers, our products, or our internal systems. It applies to everyone. Even if you are not on an on-call rotation, you need to know how to report an incident and where to look for updates.

## What counts as an incident?

An incident is any unplanned event that degrades a customer-facing service (FleetSight, OTA firmware delivery via Skylift, the support portal), exposes or might expose sensitive data, or stops a production line at one of our own sites. If you're not sure, report it anyway. We would much rather close a false alarm than find out about a real problem three days late.

## How to report

1. Post in **#inc-report** on Slack with what you saw, when, and where.
2. If it's a suspected security issue (phishing you clicked, a lost laptop, data sent to the wrong person), also email security@acmerobotics.example.
3. If you are on a customer site and a robot behaved unsafely, follow the Robot Cell Safety Standard first (stop, lock out, make safe), then report.

The on-call Incident Commander (IC) will triage and assign a severity.

## Severity levels

| Severity | Definition | Examples | Page / ack target |
|---|---|---|---|
| SEV1 | Critical customer impact or active security breach | FleetSight down for all customers; confirmed data exposure; Skylift pushing bad firmware | 15 minutes |
| SEV2 | Major degradation, partial outage, or contained security event | Telemetry delayed for many customers; one region degraded | 30 minutes |
| SEV3 | Minor issue, workaround exists | A single dashboard broken; delayed alert emails | next business day |
| SEV4 | Cosmetic / internal annoyance | Typo in a UI string | backlog |

For SEV1 incidents, the on-call engineer must acknowledge the PagerDuty page within 15 minutes, and the Incident Commander must open a dedicated Slack war room channel immediately. If the primary on-call doesn't acknowledge in time, PagerDuty escalates automatically to the secondary and then to the engineering manager on duty.

For SEV2 incidents, the on-call engineer must acknowledge the page within 30 minutes.

## Roles during an incident

**Incident Commander (IC)** - runs the incident. Decides priorities, assigns work, makes the call on rollbacks. The IC does not debug; the IC coordinates.

**Technical Lead** - leads the hands-on investigation and fix.

**Communications Lead** - writes internal updates in the war room channel every 30 minutes for SEV1 (every 60 minutes for SEV2) and works with Customer Support on customer-facing status page updates.

**Scribe** - keeps a timestamped log of decisions and actions. This log becomes the backbone of the postmortem.

For security incidents, the CISO or a delegate from the security team joins as a co-IC, and Legal is notified for any suspected exposure of customer or personal data.

## Lifecycle

1. **Detect** - alert, customer report, or employee report.
2. **Triage** - IC assigns severity and roles. War room channel named `#inc-YYYYMMDD-short-name`.
3. **Mitigate** - stop the bleeding first. Roll back, fail over, revoke credentials, disable a feature flag. Root cause can wait; customers can't.
4. **Resolve** - service restored and stable for at least 30 minutes.
5. **Review** - postmortem.

## Postmortems

Every SEV1 and SEV2 incident requires a written postmortem. The postmortem is due within 5 business days of the incident being resolved, and must be reviewed in the weekly Reliability Review meeting on Thursdays.

Postmortems are blameless. We write about what the system allowed to happen, not who to blame. A good postmortem includes:

- Summary and customer impact (who, how many, how long)
- Timeline in UTC
- Root cause and contributing factors
- What went well / what went poorly
- Action items with owners and due dates, tracked in Jira under the `POSTMORTEM` label

Security postmortems are classified confidential and shared only with engineering leadership and the security team.

## Customer communication

Customer Support owns the status page. For SEV1 incidents affecting FleetSight, the first public status update must be posted within 30 minutes of the incident being declared, even if all it says is "we're investigating." Contractual SLA notifications for specific customers are handled by Customer Success together with Finance.

## After hours

The on-call schedule lives in PagerDuty. Swap shifts with a teammate in PagerDuty directly; don't just agree on Slack and hope. On-call engineers receive an on-call stipend per week of primary coverage, described in the engineering on-call guide.
