IT SECURITY POLICY
==================

Policy SEC-001 | Version 3.1 | Effective 2026-06-15
Owner: Information Security (Omar Haddad, CISO)
Approved by: Executive Leadership Team

## 1. Purpose and scope

This policy sets the minimum security rules for everyone who uses Acme Robotics systems, data or devices: employees, contractors, interns and temporary staff. It covers corporate IT (laptops, email, Slack, Workday, NetSuite), engineering systems (GitHub, AWS, CI/CD, Skylift), and operational technology on our own production floors (robot controllers, PLCs, test stations).

Security is not just an IT problem at a robotics company. A compromised laptop can leak a customer's cell layout; a compromised CI pipeline can ship bad firmware to robots that move heavy things near people. Please read this carefully.

Revision 3.1 includes changes made after the April 2026 security incident review, in particular sections 6 and 7.

## 2. Accounts and authentication

2.1 **Single sign-on.** All corporate applications use Okta single sign-on. If an application can't use Okta, it needs a written exception from Information Security.

2.2 **Multi-factor authentication.** MFA is required for every account, with no exceptions. Use Okta Verify push or a hardware security key. SMS codes are not allowed. Engineers with production AWS access, and all platform admins, must use a hardware security key (YubiKey), which IT provides.

2.3 **Passwords.** Passwords must be at least 14 characters long. Passphrases are encouraged ("four random words" beats "P@ssw0rd!"). We do not force periodic password changes, but you must change your password immediately if you suspect it was exposed. Do not reuse your Acme password on any other site.

2.4 **Password manager.** Use the company-provided 1Password account for all work credentials. Shared team credentials belong in a shared 1Password vault, never in a Slack message, a spreadsheet, or a sticky note on a test station.

2.5 **Account sharing** is prohibited. Every person gets their own account, including on production-floor HMIs where that is technically possible.

## 3. Devices

- Company laptops are managed by Jamf (Mac) or Intune (Windows) and must have full-disk encryption, automatic OS updates and the CrowdStrike endpoint agent running. Don't disable them.
- Screens lock automatically after 5 minutes of inactivity. Lock your screen manually (Win+L / Ctrl+Cmd+Q) whenever you walk away.
- Personal phones may be used for Slack, email and Okta Verify if they have a passcode and a supported OS version. Don't store customer files on personal devices.
- Personal laptops and personal USB drives may not be connected to the corporate network or to any robot controller.
- Report a lost or stolen device to IT within 1 hour of noticing, at #it-help or ext. 4357. IT can wipe it remotely, which is a lot better than the alternative.

## 4. Data classification

| Class | Examples | Rules |
|---|---|---|
| Public | product overview, press releases, careers page | can be shared freely |
| Internal | policies, runbooks, org charts, internal roadmaps | employees and approved contractors only |
| Confidential | customer contracts, pricing, financial results, salary data, security incident details, M&A | need-to-know only; encrypted at rest; no personal email or personal cloud |
| Restricted | customer cell designs, source code signing keys, production credentials | named individuals only; access logged and reviewed quarterly |

If you aren't sure how something is classified, treat it as confidential and ask the owner.

Customer data (telemetry, cell layouts, CAD files they send us) belongs to the customer. Use it only for the purpose the customer gave it to us.

## 5. Email, Slack and phishing

Most attacks against us start with an email. Use the "Report Phish" button in Outlook for anything suspicious. You will never be in trouble for reporting a real email that turned out to be fine. You might be in trouble for wiring money because an email that looked like the CFO asked nicely.

Finance will never change a vendor's bank details based on an email alone; all changes are verified by a phone call to a number already on file.

## 6. Engineering and cloud security

6.1 **Secrets.** Credentials, API tokens, private keys and connection strings must never be committed to any source repository, including private repositories and personal forks. All secrets live in AWS Secrets Manager or HashiCorp Vault and are injected at runtime. Secret scanning (GitHub Advanced Security) is enabled on every repository in the Acme GitHub organization, and forking of organization repositories to personal accounts is disabled.

6.2 **Token lifetime.** Long-lived static service tokens are being phased out. New service-to-service authentication must use short-lived credentials (OIDC federation or STS tokens with a maximum lifetime of 1 hour). Any remaining static token must be scoped to the minimum required permissions and rotated at least every 90 days.

6.3 **Production access.** Access to production AWS accounts is granted through AWS IAM Identity Center with just-in-time elevation, approved by a second engineer and limited to 4 hours. Standing admin access is not allowed.

6.4 **Code review.** All changes to production services and to ArcOS firmware require review by at least one other engineer. Changes to the safety controller code require review by two engineers, one of whom must be on the Functional Safety team.

6.5 **Firmware signing.** ArcOS images are signed with keys stored in a hardware security module. Nobody, including the CTO, can export a signing key. Skylift refuses to deliver unsigned images.

## 7. Logging and monitoring

Security-relevant logs (Okta, AWS CloudTrail, GitHub audit logs, endpoint telemetry) are sent to our SIEM and retained for 13 months. The security team reviews high-severity alerts 24/7 through a managed detection partner. Alerts on anomalous API usage against FleetSight were added in May 2026.

## 8. Operational technology (production floor)

- The production network is segmented from the corporate network. Test stations and robot controllers sit on the OT VLAN with no direct internet access.
- Firmware is loaded onto controllers on the floor only from the signed internal image server, never from a laptop download folder.
- Vendors who need remote access to OT equipment connect through the jump host with a time-limited session approved by the Manufacturing Engineering manager.

## 9. Third parties and new tools

Before buying or signing up for any new SaaS tool that will hold Acme or customer data, submit a vendor security review request in Jira (project SECREV). Free trials count. A typical review takes 5 to 10 business days.

Do not paste confidential or restricted data into public AI chatbots or online converters. Approved AI tools are listed on the IT intranet page.

## 10. Remote and travel

When working outside an Acme site, follow the Remote Work Policy. Don't work on confidential data on public Wi-Fi without the Acme VPN (GlobalProtect). On customer sites, connect only to networks the customer explicitly provides for our use.

## 11. Incidents

Report suspected security incidents immediately - see the Incident Response Process. Examples: you clicked a phishing link, you sent a file to the wrong person, you found a credential in a repo, or a device is missing. Speed matters more than certainty.

## 12. Enforcement and exceptions

Violations of this policy may lead to loss of access and disciplinary action, up to and including termination. Exceptions must be requested in writing through the SECREV Jira project, approved by the CISO, and reviewed at least annually.

Questions: #security on Slack or security@acmerobotics.example.
