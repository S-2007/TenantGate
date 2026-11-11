CONFIDENTIAL - Engineering leadership & Security only

# Security Incident Postmortem: Exposed FleetSight ingestion token

| Field | Value |
|---|---|
| Incident ID | SEC-ACME-2026-0417 |
| Severity | SEV1 (security) |
| Status | Closed - all action items tracked |
| Incident Commander | Omar Haddad (CISO), co-IC Arjun Mehta (FleetSight Platform) |
| Author | Sofia Brandt, Security Engineering |
| Date of incident | 2026-04-17 |
| Postmortem published | 2026-04-24 |

## Summary

On April 17, 2026, an unauthorized party used a FleetSight ingestion gateway service token that had been exposed in a public GitHub repository. Using the token, the attacker called the gateway's device-metadata API and retrieved robot metadata (serial numbers, model, firmware version, site name and approximate site location) for robots belonging to 3 customers, a total of 212 robots. The attacker also attempted to publish telemetry messages, which were rejected.

No telemetry history, cell programs, CAD files or personal data were accessed. No robot was commanded or affected in any way; the ingestion gateway has no command path to robots. The exposure lasted roughly 9 hours from first malicious use to token revocation.

## Customer impact

- 3 customers notified by Customer Success and Legal on April 18 within contractual timelines.
- 212 robot metadata records exposed.
- No downtime. FleetSight was fully available throughout.

## Root cause

The root cause was a long-lived static FleetSight gateway service token, with far broader read scope than the job needed, that a contractor committed in plaintext to a CI configuration file in a personal fork of the fleetsight-edge-sim repository; because the fork lived outside the Acme GitHub organization, our secret scanning never ran on it.

In more detail:

1. **The token.** The `svc-edge-sim` token was created in 2023 for a load-testing simulator. It was a static bearer token with no expiry, and it had been granted the `metadata:read:*` scope (all customers) instead of a scope limited to the internal test tenant. Nobody had reviewed it since creation.
2. **The commit.** On April 3, a contractor working on simulator performance forked `fleetsight-edge-sim` to a personal GitHub account to experiment with a different CI runner, and hard-coded the token in `.ci/runner.yml` to get the job to authenticate. The fork was public by default.
3. **The detection gap.** GitHub secret scanning was enabled on repositories inside the Acme organization only. Forks to personal accounts were allowed by the organization settings, and we had no monitoring for Acme credentials appearing in public code outside our organization.

### Contributing factors

- The device-metadata API had no anomaly alerting on request volume or on a token suddenly querying many customers.
- The gateway service token format did not include a recognizable prefix, so GitHub's partner secret-scanning program could not detect it on public repositories.
- Contractor onboarding did not cover our rules about forks and secrets clearly; the contractor believed the fork was private.

## Timeline (all times UTC)

**2026-04-03 15:12** - Contractor pushes commit with the token to a public personal fork.

**2026-04-17 02:41** - First request using the token from an IP address in a hosting provider range not associated with Acme. Requests start enumerating device metadata, customer by customer.

**2026-04-17 02:41 - 07:30** - Approximately 1,900 metadata requests. Attempts to POST telemetry are rejected (token lacked write scope).

**2026-04-17 11:20** - Arjun Mehta notices an unusual spike in `device-metadata` API traffic on the FleetSight dashboard during a routine capacity review and asks in #fleetsight-platform.

**2026-04-17 11:34** - Security on-call (Sofia Brandt) paged. Acknowledged 11:38.

**2026-04-17 11:52** - SEV1 security incident declared. War room `#inc-20260417-edge-token` opened. Omar Haddad joins as IC.

**2026-04-17 11:58** - Token `svc-edge-sim` revoked at the gateway. Malicious requests begin failing with 401.

**2026-04-17 12:40** - GitHub code search finds the token in the public fork. Contractor contacted; fork deleted at 13:05. GitHub support asked to purge cached views.

**2026-04-17 14:30** - Log analysis confirms the scope of access: metadata only, 3 customers, 212 robots. No access with other credentials.

**2026-04-17 16:00** - Legal and Customer Success brief on notification plan.

**2026-04-18 15:00** - All 3 affected customers notified.

**2026-04-20** - All remaining static service tokens on the FleetSight gateway inventoried (17 found); 11 revoked as unused, 6 rotated and re-scoped.

**2026-04-24** - Postmortem reviewed at Reliability Review.

## What went well

- Once noticed, containment was fast: 6 minutes from SEV1 declaration to token revocation.
- Least-privilege at the write path worked; the token could not publish telemetry, so no false data entered customer dashboards.
- The scribe log was excellent and made this document straightforward to write.
- Customer notifications went out well inside contractual timelines, and all three customers acknowledged without escalation.

## What went poorly

- Detection was accidental. The attacker had about 9 hours before a human happened to look at a dashboard.
- The token had sat unreviewed with an all-customer scope for almost three years.
- It took over 40 minutes to find where the token had leaked, because we had no tooling to search public code for Acme credentials.

## Action items

| # | Action | Owner | Due | Status |
|---|---|---|---|---|
| 1 | Disable forking of Acme org repositories to personal accounts | Sofia Brandt | 2026-04-19 | Done |
| 2 | Replace all static gateway service tokens with short-lived OIDC credentials (max 1 hour) | Arjun Mehta | 2026-06-30 | Done |
| 3 | Add `acmefs_` prefix to all gateway tokens and register the pattern with GitHub secret scanning partner program | Sofia Brandt | 2026-05-15 | Done |
| 4 | Datadog anomaly alerts on device-metadata API: volume, distinct customers per token | FleetSight Platform | 2026-05-08 | Done |
| 5 | Quarterly review of all service tokens and scopes | Security Engineering | recurring | In place |
| 6 | Update contractor onboarding with secrets and repository rules; require signed acknowledgment | People Ops + Security | 2026-05-31 | Done |
| 7 | Update IT Security Policy sections 6 and 7 | Omar Haddad | 2026-06-15 | Done (v3.1) |
| 8 | Subscribe to a public code-leak monitoring service | Security Engineering | 2026-07-31 | Done |

## Lessons

The interesting failure here was not the commit itself - people will always occasionally put a secret where it doesn't belong. The failure was that the system let a single mistake become an incident: a token that never expired, could read everything, and lived in a place none of our controls could see. Every one of those three conditions has now been removed.

## Distribution

Shared with: engineering leadership, security team, FleetSight Platform team, Legal. Do not forward. Customer-facing descriptions of this incident must come from Legal-approved language only.
