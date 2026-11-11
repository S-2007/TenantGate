CONFIDENTIAL — Security Postmortem — Engineering & Admin only

# Postmortem: Exposed CI credential used to read merchant onboarding exports

| Field | Value |
|---|---|
| Incident ID | SEC-LUMEN-2026-0508 |
| Severity | SEV1 (security) |
| Status | Resolved; action items in progress |
| Incident Commander | Kofi Mensah (Head of Security) |
| Tech Lead | Wren Takahashi (Staff Security Engineer) |
| Author | Wren Takahashi, reviewed by Ravi Menon |
| Date of incident | May 8, 2026 (detected) / exposure window Apr 29 – May 8 |
| Postmortem published | May 13, 2026 |

## Summary

A long-lived AWS IAM access key belonging to the `ci-keystone-deployer` service user was printed in plain text in a GitHub Actions build log after a developer enabled a debug flag. The log was retained in a public-facing fork of an internal repository for nine days. An unknown external party used the key to list and download objects from the S3 bucket `lumen-keystone-exports-stg`, which — contrary to policy — contained a copy of real merchant onboarding exports used for a data-migration test.

**Root cause:** the `keystone-deploy` CI workflow used a static IAM access key stored as a repository secret instead of short-lived OIDC credentials, and the `DEBUG_AWS=1` flag caused the AWS CLI wrapper script to echo the full environment, including the key, into the build log. Masking did not apply because the wrapper base64-encoded the environment before printing it. A contributing cause was that production-derived merchant data had been copied to a staging bucket without approval.

## Impact

- 1,214 merchant onboarding records were downloaded: business legal name, EIN, business address, owner name and owner date of birth.
- **No** SSNs, bank account numbers, card data or ID document images were in the exported files (those stay in the KYC vault).
- No evidence of access to production systems, Prism or the payments path.
- Notification to Harbor Plains Bank on May 9; merchant notification letters sent May 22 with 12 months of free credit monitoring for affected owners.

## Timeline (all times CT)

| When | What happened |
|---|---|
| Apr 29, 16:42 | Engineer sets `DEBUG_AWS=1` on a branch to troubleshoot a failing deploy. Build log contains the base64 environment dump. |
| Apr 29, 17:05 | Branch pushed to a fork that had been mistakenly set to public in March. |
| May 3 – May 7 | Access key used from three IP addresses (hosting providers in two countries). `ListObjects` and `GetObject` calls on `lumen-keystone-exports-stg`. |
| May 8, 08:14 | GuardDuty alert: `UnauthorizedAccess:IAMUser/InstanceCredentialExfiltration.OutsideAWS`. |
| May 8, 08:17 | Security on-call (Wren Takahashi) acks the page — 3 minutes. |
| May 8, 08:31 | SEV1 security incident declared; private channel opened. |
| May 8, 08:40 | Access key deactivated; IAM user attached to deny-all policy. |
| May 8, 09:25 | Public fork identified and made private; GitHub support asked to purge cached logs. |
| May 8, 11:50 | CloudTrail analysis confirms scope: one bucket, 47 objects, 1,214 merchant records. |
| May 8, 14:00 | Legal (Sarah Lindqvist) and Compliance briefed; bank partner notification drafted. |
| May 8, 18:30 | All other static CI keys (11 in total) rotated as a precaution. Incident moved to "contained". |
| May 9, 10:00 | Bank partner notified. |
| May 12 | Incident resolved after confirming no further use of any rotated credential. |

## What went well

- GuardDuty detection worked and the page was acknowledged within the SEV1 target.
- Containment of the key took 26 minutes from page.
- CloudTrail data events were enabled on the bucket, so we could say exactly what was taken.

## What went badly

- The static key had existed since 2023 and was flagged in a 2025 audit finding that was still open.
- Nobody noticed the fork was public for two months; we had no alert for repository visibility changes.
- Real merchant data sat in staging for six weeks after the migration test ended.

## Action items

| # | Action | Owner | Due | Status |
|---|---|---|---|---|
| 1 | Move all CI pipelines to GitHub OIDC → AWS role assumption; delete static keys | Diego Ferreira | 2026-06-15 | Done |
| 2 | Remove `DEBUG_AWS` env dump from wrapper script; add log scanning for key patterns | Wren Takahashi | 2026-05-29 | Done |
| 3 | Org policy: block public forks of internal repos; alert on visibility change | Kofi Mensah | 2026-05-22 | Done |
| 4 | Automated scan + 7-day TTL for staging buckets with PII-like data | Anneke Sorensen | 2026-07-31 | In progress |
| 5 | Close out 2025 audit finding AUD-25-014 with evidence | Kofi Mensah | 2026-06-30 | Done |

*Do not forward. Distribution limited to Engineering and Admin per the Incident Response Process, section 8.*
