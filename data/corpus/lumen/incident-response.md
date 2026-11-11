# Incident Response Process

**Owner:** SRE (Hollis Abernathy, SRE Manager) with Security (Kofi Mensah)
**Applies to:** everyone. Yes, including you in Marketing.
_Last updated: Aug 21, 2026_

---

Lumen moves money for tens of thousands of small businesses. When Lumen Pay is down, a food truck can't take cards at lunch. When payouts are late, someone misses payroll. This document is how we respond — fast, calmly, and in the open.

This is the **operational** process for handling an incident while it is happening and in the days right after. Specific technical steps are in the runbooks (e.g. payment gateway failover, ledger reconciliation). Security incidents follow this process plus the extra steps in section 8.

## 1. What counts as an incident

An incident is any unplanned event that degrades or threatens a Lumen service, merchant funds, or data. Examples:

- Card authorization error rate above 2% for 5 minutes
- Payouts not sent by the scheduled cutoff
- Prism ledger out of balance
- Lumen Hub login failures
- Suspected unauthorized access to systems or data
- A bank partner or card network notifies us of a problem

If you're not sure, **declare it anyway.** We'd much rather downgrade a false alarm than discover an hour later that something was burning.

## 2. Severity levels

| Severity | Definition | Examples | Page/ack target | Updates |
|---|---|---|---|---|
| SEV1 | Critical: broad merchant impact, funds at risk, or confirmed data breach | card authorizations failing across merchants; payouts batch failed; confirmed breach | 5 minutes | every 30 min |
| SEV2 | Major: significant degradation or one large merchant segment affected | Same-Day Payouts delayed; Beacon scoring timing out for >10% of transactions | 15 minutes | every 60 min |
| SEV3 | Minor: limited impact, workaround exists | QuickBooks export failing; one reader firmware bug | 1 business hour | daily |
| SEV4 | Cosmetic / internal tooling | typo on a dashboard; internal admin page slow | next business day | as needed |

For a SEV1 incident, the on-call engineer must acknowledge the page within 5 minutes. If the primary doesn't ack within 5 minutes, PagerDuty escalates automatically to the secondary, then to the Engineering Manager on duty at 10 minutes.

## 3. How to declare an incident

1. In Slack, type `/incident declare` in any channel.
2. Pick a severity (your best guess is fine) and write a one-line summary.
3. The bot creates a channel `#inc-YYYYMMDD-short-name`, pages the right on-call via PagerDuty, and opens a Jira ticket in the INC project.
4. If Slack is down, call the SRE on-call hotline (number pinned on the #incidents channel topic and printed on the back of your badge) and use the Zoom bridge "Lumen-War-Room".

Merchant Support: if you see three or more tickets on the same symptom within 15 minutes, declare an incident. Don't wait for engineering to notice.

## 4. Roles

Every SEV1 and SEV2 has these roles. One person can hold more than one role on a SEV3.

**Incident Commander (IC)** — runs the incident. Decides priorities, assigns tasks, calls for help, decides when it's over. The IC does *not* debug. For SEV1, the IC comes from the IC rotation (senior engineers and EMs who have done the IC training).

**Tech Lead** — leads the hands-on investigation and fix. Usually the primary on-call for the affected service.

**Communications Lead** — writes internal updates in #incidents and coordinates with Merchant Support and, for SEV1, with the Status Page owner and Communications (Tasha Greenleaf).

**Scribe** — keeps the timeline in the incident channel: what happened, who decided what, and when. Good timelines make the postmortem easy.

For anything involving merchant funds, add a **Treasury liaison** (Finance on-call) and, for possible regulatory reporting, the **Compliance liaison**.

## 5. During the incident

- **Stabilize first, root cause later.** Roll back, fail over, turn off the feature flag. Use the runbooks.
- Keep all discussion in the incident channel. No DMs about the incident — the scribe can't see them.
- Post updates on the schedule in section 2 even if the update is "no change, still investigating."
- Don't make customer-facing statements on social media or to press. Comms does that.
- Before any manual change to the Prism ledger or a payout file, get a second engineer to review it on the call ("two-person rule"). Every manual ledger entry must be recorded in the INC ticket.

### 5.1 Status page and merchant communication

| Severity | Status page | Merchant email |
|---|---|---|
| SEV1 | within 15 minutes of declaration | if impact > 1 hour, or funds delayed |
| SEV2 | within 30 minutes if merchant-visible | case by case |
| SEV3/4 | no | no |

### 5.2 Escalation contacts

- Engineering Manager on duty (PagerDuty schedule "EM-Duty")
- CTO Ravi Menon — all SEV1s, informed within 30 minutes
- Head of Security Kofi Mensah — any suspected security incident
- CFO Tobias Kwan — if merchant funds are delayed more than 4 hours
- General Counsel Sarah Lindqvist — any confirmed data exposure

## 6. Ending the incident

The IC declares the incident **resolved** when the service is stable and merchant impact has stopped. Then:

1. Post a final update in #incidents and on the status page.
2. Run `/incident resolve` to timestamp and close the channel for new discussion.
3. Create follow-up tickets for any temporary fixes (feature flags left off, manual workarounds).
4. Hand off any ongoing merchant reconciliation to Payments Ops.

## 7. Postmortems

Every SEV1 and SEV2 gets a written postmortem. SEV3s get one if the IC asks for it.

The postmortem is due within 3 business days of the incident being resolved. Use the template in Confluence (Engineering > Postmortems > Template). It must include:

- Summary and impact (merchants affected, dollars delayed, duration)
- Timeline (from the scribe's notes)
- Root cause and contributing factors
- What went well / what went badly
- Action items, each with an owner and a due date in Jira

Postmortems are **blameless**. We ask "how did our systems let this happen?", not "who messed up?". Postmortems are reviewed at the weekly Reliability Review on Wednesdays at 1 p.m. CT. Action items from SEV1 postmortems are tracked by the SRE Manager until closed; overdue items are reported monthly to the CTO.

## 8. Security incidents — extra steps

If the incident involves (or might involve) unauthorized access, data exposure, malware, or a leaked credential:

1. Page **Security on-call** in addition to the service on-call. Security takes the IC role or names a deputy IC.
2. **Preserve evidence.** Don't wipe or reimage machines, delete logs, or rotate everything blindly before Security agrees. Snapshot first.
3. Revoke and rotate credentials that are known to be exposed — that part can't wait.
4. Move the discussion to a private incident channel. Security incident channels are restricted.
5. Legal and Compliance decide on notifications to the bank partner, card networks, regulators, and merchants. Card networks and our bank partner may require notice within strict timeframes, so loop in Legal early.
6. The security postmortem is restricted to Engineering and Admin readers and stored in the Security space, not the general Postmortems space.

## 9. Practice

SRE runs a game day every quarter (most recently a simulated failover of the primary payment gateway on July 16). Everyone on an on-call rotation must attend at least two game days a year. New ICs shadow two real incidents before leading one.

## 10. Quick reference

```
Declare:      /incident declare
SEV1 ack:     5 minutes
Updates:      SEV1 every 30 min, SEV2 every 60 min
Postmortem:   due within 3 business days
War room:     Zoom "Lumen-War-Room"
Security:     page "Security On-Call" in PagerDuty
```

Questions or suggestions: #sre-team.
