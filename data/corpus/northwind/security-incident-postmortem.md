CONFIDENTIAL - Security / Engineering leadership. Contains details of a security vulnerability. Do not forward outside the distribution list.

# Post-Incident Review: Unauthorized Access to Appointment Records via Scheduling API

| Field | Value |
|---|---|
| Incident ID | SEC-NORTHWIND-2026-0322 |
| Severity | SEV1 (confirmed PHI exposure) |
| Date detected | March 22, 2026 |
| Date resolved | March 22, 2026 (fix deployed); investigation closed April 3, 2026 |
| Incident Commander | Tomas Reyes (SRE) |
| Technical Lead | Abby Nakamura (Scheduling team) |
| Security Lead | Jordan Pike (CISO) |
| Privacy Lead | Nadia Farouk (CPO) |
| Postmortem author | Abby Nakamura, with InfoSec |
| Status | Final - reviewed by CTO 2026-03-31 |

## Summary

For about 51 hours between March 20 and March 22, 2026, an authorization flaw in the Northwind Anywhere scheduling API allowed a signed-in patient account to retrieve other patients' appointment details by changing the appointment ID in a request. An external security researcher reported the issue through our vulnerability disclosure program. Our investigation found that two accounts - the researcher's and one other account that we were unable to attribute - accessed appointment records that did not belong to them. In total, 1,937 appointment records belonging to 1,612 patients were accessed improperly.

Exposed fields: patient first and last name, date of birth, appointment date/time, clinic location, visit type (e.g. "behavioral health follow-up"), and clinician name. No clinical notes, diagnoses, SSNs, insurance numbers or payment data were exposed.

## Root cause

The root cause was a missing patient-ownership authorization check in the GET /v2/appointments/{id} endpoint of the scheduling API, introduced in release 4.18.0 when the authorization middleware was refactored.

Before 4.18.0, each endpoint called a helper, `assertPatientOwns(appointment)`, inside its handler. The refactor moved authorization into a shared middleware that is configured per route in a YAML policy file. The policy entry for `/v2/appointments/{id}` was written as `auth: authenticated` instead of `auth: patient_owner`. With `authenticated`, any valid patient session token was accepted, and the old in-handler check had been deleted as "now redundant."

### Contributing factors

1. **Tests did not cover the negative case.** The endpoint's tests checked that a patient could read their own appointment, but none checked that a patient could not read someone else's.
2. **Sequential IDs.** Appointment IDs are sequential integers, which made enumeration trivial.
3. **Rate limiting was per-IP only.** The unattributed account spread requests across many IP addresses, staying under the limit of 300 requests per minute per IP.
4. **Code review gap.** The 4.18.0 refactor touched 46 route policies in one pull request. Reviewers focused on the middleware code, not the YAML policy entries.
5. **No anomaly alert** existed for a single account reading appointments belonging to many different patients.

## Timeline (all times Central)

| When | What happened |
|---|---|
| Mar 20, 14:05 | Release 4.18.0 deployed to production. |
| Mar 21, 02:40 | First improper access by the unattributed account (identified later from logs). |
| Mar 21, 02:40 - Mar 22, 06:15 | Unattributed account retrieves 1,874 appointment records using rotating IPs. |
| Mar 22, 13:58 | Researcher retrieves 63 records while testing, then stops. |
| Mar 22, 15:12 | Researcher submits a report to security@ through the disclosure program. |
| Mar 22, 15:31 | InfoSec on-call (Kayla Osei) confirms the issue and pages SRE; SEV1 declared. |
| Mar 22, 15:38 | PagerDuty acknowledged by Tomas Reyes; IC assigned. |
| Mar 22, 15:52 | Mitigation: WAF rule blocks /v2/appointments/{id} for patient tokens; app shows appointment list from cached data. |
| Mar 22, 17:20 | Hotfix 4.18.1 deployed restoring the `patient_owner` policy and in-handler check. |
| Mar 22, 17:45 | WAF rule removed; endpoint verified with negative tests. |
| Mar 22, 19:00 | Unattributed account disabled; all its sessions revoked. |
| Mar 23 - Apr 2 | Log review to identify every affected record; outside forensic firm engaged on Mar 24. |
| Apr 3 | Privacy Office completes breach risk assessment; determination: reportable breach. |

## Impact

- 1,612 patients had appointment information exposed. Because visit types like "behavioral health" were exposed, the Privacy Office treated the incident as a reportable breach under HIPAA even though no clinical notes were involved.
- Individual notification letters were mailed on April 17, 2026. Because more than 500 Minnesota residents (1,128) were affected, HHS was notified through the breach portal at the same time and media notice was provided in Minnesota, as required.
- Credit monitoring was not offered because no financial identifiers were exposed; a call center line was set up for affected patients.
- Engineering cost: about 310 engineer-hours for the response and remediation work.

## What went well

- The researcher's report was triaged in 19 minutes.
- The WAF mitigation stopped exposure within 21 minutes of the SEV1 declaration, without taking the app down.
- Access logs had full request detail, which made it possible to identify exactly which records were accessed.

## What went poorly

- The flaw was live for over two days before anyone noticed.
- We had no way to detect one account reading many patients' records.
- The first draft of the patient notification letter used technical language that the Privacy Office had to rewrite.

## Where we got lucky

- The researcher reported responsibly and stopped testing after 63 records.
- The exposed endpoint did not return clinical notes or insurance information.

## Action items

| # | Action | Owner | Due | Status |
|---|---|---|---|---|
| 1 | Add negative authorization tests for every patient-facing endpoint; block merge if missing | Abby Nakamura | 2026-04-30 | Done |
| 2 | Replace sequential appointment IDs with random UUIDs in public APIs | Scheduling team | 2026-06-30 | Done |
| 3 | Per-account rate limiting and an alert for "one account, many patients" | SRE (Tomas Reyes) | 2026-05-15 | Done |
| 4 | Route policy changes require review by a Security Champion (CODEOWNERS rule) | InfoSec | 2026-04-15 | Done |
| 5 | Default-deny: middleware rejects any route without an explicit policy | Platform team | 2026-07-31 | In progress |
| 6 | Annual third-party penetration test to include authorization testing of all APIs | Jordan Pike | 2026-09-30 | Scheduled |
| 7 | Plain-language templates for patient breach notifications | Privacy Office | 2026-05-31 | Done |

## Lessons

Refactors that move security checks are security changes and must be reviewed as such. "Authenticated" is not the same as "authorized."
