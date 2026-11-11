# Incident Response Process

**Owner:** Digital Health Site Reliability Engineering (SRE) + IT Operations
**Approved by:** Elena Sorensen, Chief Technology Officer
**Last updated:** 2026-05-19

This document describes how Northwind Health detects, declares, runs and closes out operational incidents affecting our technology: Northwind Anywhere, the EHR and its interfaces, clinic networks and phones, Okta, and corporate systems. It applies to everyone, not just engineers - a front-desk coordinator who notices that check-in is broken at three clinics is often our first alert.

Security incidents (suspected breach, malware, lost device with PHI) follow this same process **plus** the Security Incident Addendum owned by the Information Security team. If you suspect patient data has been exposed, always page Security as well.

This is the live process. Post-incident reviews of specific events are written up separately.

---

## 1. Why this matters in healthcare

When our systems are down, patients wait, clinicians chart on paper, prescriptions are delayed and lab results can be missed. A 30-minute EHR outage across all clinics affects roughly 1,400 scheduled visits. We treat clinical-impacting incidents with the urgency they deserve, and we prefer to over-declare and then downgrade rather than under-declare.

## 2. Severity levels

| Severity | Definition | Examples |
|---|---|---|
| SEV1 | Patient care is disrupted at multiple clinics, or a core patient-facing service is fully down, or confirmed exposure of PHI | EHR unavailable; Northwind Anywhere video visits failing for all patients; e-prescribing down everywhere; confirmed data breach |
| SEV2 | Major degradation, or one clinic fully down, or a workaround exists but is painful | Lab interface delayed over 1 hour; one care hub has no network; login failures for 20%+ of users |
| SEV3 | Minor impact, limited users, easy workaround | Printing broken in one clinic; slow reports; single integration retrying |
| SEV4 | No current user impact | Failed backup that succeeded on retry; certificate expiring in 14 days |

If you are not sure between two levels, pick the higher one.

## 3. How to raise an incident

ANYONE can raise an incident:

- **Clinical and front-line staff:** call the IT Service Desk at ext. 4357 and say "I want to report a possible outage." The Service Desk is staffed 24/7.
- **Digital Health / IT staff:** type `/incident` in the #incidents Slack channel. The bot opens a PagerDuty incident, creates a dedicated channel (`#inc-YYYYMMDD-shortname`) and a Jira ticket in the INC project.
- Automated monitoring (Datadog, AWS CloudWatch, the interface engine's queue alerts) pages the on-call directly.

## 4. Response targets

Paging and acknowledgment targets are tracked in PagerDuty and reviewed monthly.

| Severity | Ack target | Incident Commander assigned | First status update | Update cadence |
|---|---|---|---|---|
| SEV1 | 10 minutes | within 15 minutes | within 20 minutes | every 30 minutes |
| SEV2 | 30 minutes | within 45 minutes | within 1 hour | every 60 minutes |
| SEV3 | 4 business hours | as needed | next business day | daily |
| SEV4 | next business day | n/a | n/a | n/a |

For SEV1 incidents, the on-call responder must acknowledge the page within 10 minutes. If the primary on-call does not acknowledge in time, PagerDuty escalates automatically to the secondary on-call, then to the SRE manager on duty, then to the CTO.

## 5. Roles

**Incident Commander (IC).** Runs the incident. Does not debug. Makes decisions, keeps people focused, assigns work and decides when to escalate or downgrade. For SEV1 the IC comes from the trained IC rotation (currently 14 people across SRE, IT Operations and Clinical Applications).

**Technical Lead.** Leads the actual investigation and fix. Usually the on-call engineer for the affected system.

**Communications Lead.** Writes the status updates. For SEV1 and SEV2 this person posts to the #incident-updates channel, updates the internal status page, and coordinates with Clinical Operations so clinic managers know what to tell patients.

**Clinical Liaison.** Required for SEV1 incidents that affect patient care. A nurse informaticist or clinic operations leader who decides when to activate downtime procedures (paper charting, downtime prescription pads, manual scheduling lists) and tells clinics when it is safe to resume normal work.

**Scribe.** Keeps a timeline in the incident channel. The timeline becomes the backbone of the post-incident review.

## 6. Running the incident

1. **Acknowledge** the page and join the incident channel.
2. **Assess** impact: which clinics, how many users, is patient care affected, is PHI at risk?
3. **Declare severity** and assign the IC (for SEV1/SEV2).
4. **Stabilize first, root-cause later.** Roll back, fail over, or disable the feature. Use the runbooks in the SRE wiki.
5. **Communicate** on the cadence in section 4, even if the update is "no change."
6. **Activate downtime procedures** if the Clinical Liaison decides it is needed. Clinics have printed downtime binders at the front desk.
7. **Resolve** when the service is restored and stable for at least 30 minutes.
8. **Hand off** follow-up work as Jira tickets in INC before the channel goes quiet.

### 6.1 Escalating to leadership
For any SEV1, the IC notifies the CTO and the VP of Clinical Operations within 30 minutes of declaration. For a suspected PHI exposure, the IC also notifies the CISO and the Privacy Officer immediately, regardless of severity.

### 6.2 Vendor escalations
Many of our critical systems involve vendors (EHR hosting, video, telephony, clearinghouse). Vendor contact numbers and contract support tiers are in the "Vendor Escalation Matrix" page. Open a vendor ticket at their highest priority for any SEV1 and ask for a bridge.

## 7. Communications templates

Status update template (copy into #incident-updates):

```
[SEV1] Northwind Anywhere video visits failing
Impact: patients cannot join video visits; ~120 visits/hour affected
Status: Investigating / Identified / Monitoring / Resolved
Workaround: clinicians converting to phone visits
Next update: 14:30 CT
IC: <name>
```

Avoid guessing about root cause in status updates. Say what we know and what we're doing.

## 8. After the incident

Every SEV1 and SEV2 requires a written post-incident review (postmortem). The postmortem is due within 7 business days of incident resolution. The IC owns making sure it is written; the Technical Lead usually drafts it.

A postmortem must include:

- a timeline with timestamps (in Central Time)
- customer and patient impact, including number of affected clinics and visits
- the root cause and contributing factors
- what went well, what went poorly, where we got lucky
- action items with owners and due dates in Jira

Postmortems are blameless. We look for system and process causes, not someone to blame. The SRE manager reviews open postmortem action items weekly, and the CTO reviews overdue SEV1 action items monthly.

SEV3 incidents need a short summary in the Jira ticket only. SEV4 needs nothing beyond the ticket.

## 9. On-call expectations

- On-call rotations are one week, Monday 10:00 to Monday 10:00 CT.
- Carry your laptop and have a reliable connection. Your phone must be able to receive PagerDuty pushes and calls.
- On-call engineers receive an on-call stipend for each week on rotation and compensatory time off after any overnight SEV1.
- Swap shifts in PagerDuty, not just in Slack.

## 10. Drills

SRE runs a downtime drill every quarter with at least one clinic, and a tabletop exercise with the Security team twice a year. Results and gaps are tracked as Jira tickets.

## 11. Contacts

- IT Service Desk: ext. 4357
- SRE manager on duty: PagerDuty schedule "SRE-Manager"
- Security on-call: PagerDuty schedule "InfoSec-Primary"
- Privacy Office: privacy@northwindhealth.example
