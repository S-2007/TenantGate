# IT Security Policy

Document ID: SEC-POL-001 · Owner: Kofi Mensah, Head of Security
Approved by: Ravi Menon (CTO) · Last updated: 12-Apr-2026 · Review cycle: annual

---

## 1. Scope

This policy applies to every Lumen employee, contractor and intern, and to every device or account used to access Lumen systems or data. Because we're a PCI DSS Level 1 service provider and work under our bank partner's oversight, several of the rules below are not optional for us — auditors check them.

## 2. Data classification

| Class | Examples | Handling |
|---|---|---|
| Public | website, press releases, public FAQ | no restrictions |
| Internal | policies, runbooks, org charts | Lumen staff only, don't post externally |
| Confidential | salary data, M&A, contracts, financial results, security incident reports | named roles only, labeled CONFIDENTIAL |
| Restricted | cardholder data, merchant bank account numbers, SSNs, full KYC documents | only in approved systems (Prism, the KYC vault); never in Slack, email, Google Docs or tickets |

If you aren't sure, treat data as the higher class.

## 3. Accounts and authentication

- All access goes through **Okta SSO** with phishing-resistant MFA. For most staff that's Okta FastPass; engineers and anyone with production or Prism access must use a **YubiKey**.
- Passwords (where still needed) must be at least 14 characters and stored in 1Password. Don't reuse your Lumen password anywhere else.
- Never share accounts or MFA codes. Lumen IT and Security will **never** ask for your password.
- Access is reviewed quarterly by managers in the AccessHub review campaign. Access not confirmed within 10 business days is removed.

## 4. Devices

1. Only Lumen-managed laptops (enrolled in Jamf, with CrowdStrike Falcon) may access Restricted or Confidential data.
2. Disks must be encrypted (FileVault is enforced).
3. Screens lock after 5 minutes of inactivity.
4. Personal phones may use Slack, Google Workspace and Okta Verify only if enrolled in our mobile management profile.
5. Lost or stolen device? Report it to #security-help or security@lumenfinance.example **within 1 hour**.

USB storage devices are blocked on managed laptops. If you need to move files, use Google Drive.

## 5. Network and remote access

When working from anywhere other than a Lumen office, you must connect through the **Cloudflare WARP** client (our zero-trust VPN) before accessing internal systems. Public Wi-Fi is fine *only* with WARP on. Never access Restricted data from a public place where others can see your screen (cafés, planes, co-working shared tables).

Production access is through Teleport with session recording, never by direct SSH keys.

## 6. Software and third-party apps

- Install software only from the Lumen Self Service app or with an approved IT ticket.
- Any new SaaS tool, browser extension, or AI assistant that will handle Lumen data needs a Security review (Jira project SECREV) before use — even free tools and trials.
- Do not paste Confidential or Restricted data into public AI chatbots. Use the approved internal assistant.

## 7. Email and phishing

Use the "Report Phish" button in Gmail for anything suspicious. We run monthly phishing simulations; people who click three simulations in 12 months will be asked to redo security awareness training. Watch especially for fake "merchant" emails with invoice attachments and fake bank-partner wire requests.

## 8. Secrets in code

Engineers: no secrets in source code, CI logs, or tickets. Secrets go in AWS Secrets Manager. CI must use short-lived OIDC credentials, not static access keys. Pre-commit secret scanning (gitleaks) is mandatory on all repos.

## 9. Reporting

Report any suspected security incident immediately to #security-help or page Security On-Call. You won't get in trouble for reporting a mistake — you might for hiding one.

## 10. Violations

Violations can result in loss of access, disciplinary action up to termination, and where the law requires, reporting to authorities. Exceptions need written approval from the Head of Security and are reviewed every 6 months.
