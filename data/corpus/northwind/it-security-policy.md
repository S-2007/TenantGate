INFORMATION SECURITY POLICY
###########################

Document: SEC-001 Information Security Policy (employee version)
Owner: Jordan Pike, Chief Information Security Officer
Applies to: all Northwind Health workforce members - employees, contractors, students, volunteers
Last updated - April 2026

## 1. Why we have this policy

Northwind Health holds health information for more than 900,000 patients. That information is protected by HIPAA and by state privacy laws, and our patients trust us with it. Most security incidents in healthcare begin with something ordinary: a reused password, a clicked phishing link, an unlocked workstation, a laptop left in a car. This policy describes what each of us must do. The technical standards that IT and Digital Health follow are in separate standards documents.

Violations of this policy can result in loss of system access, discipline, or termination, and in some cases personal legal liability.

## 2. Accounts and passwords

- Every person gets their own account. Shared logins are prohibited, including in clinics. (Clinic workstations use tap-badge login so you do not need to share.)
- Passwords must be at least 14 characters long. Passphrases are encouraged ("four random words" style).
- Do not reuse your Northwind password on any other site.
- Passwords expire only if we suspect compromise; we do not force routine 90-day changes.
- Never share your password with anyone, including IT. The Service Desk will never ask for it.
- Store any other work credentials in the approved password manager (1Password Business). Spreadsheets, sticky notes and browser-saved passwords are not approved.

## 3. Multi-factor authentication (MFA)

MFA through Okta Verify is required for all remote access, email, Workday, the EHR when accessed outside a clinic network, and all administrative accounts.

- Only approve an MFA push that you started yourself. If you get a push you did not expect, deny it and report it to the Service Desk right away. This is how attackers get in.
- Administrators and Digital Health engineers must use a FIDO2 hardware key (YubiKey) for privileged access.
- Lost your phone? Call the Service Desk at ext. 4357 to reset MFA; you will be asked to verify your identity by video.

## 4. Devices

### 4.1 Northwind devices
- Laptops are encrypted (BitLocker or FileVault) and managed by Intune/Jamf. Do not disable security tools.
- Lock your screen whenever you step away (Windows + L or Ctrl + Cmd + Q). Clinic workstations lock automatically after 5 minutes.
- Install software only from the Company Portal / Self Service app. Need something else? Submit a software request in ServiceNow.
- Keep your laptop with you when traveling. Never leave it visible in a car.

### 4.2 Personal devices
- You may use a personal phone for email, Teams and Okta Verify only after enrolling it in Intune app protection.
- Never store PHI on a personal device, in personal email, or in personal cloud storage.
- Do not text PHI using personal SMS. Use the secure messaging feature in the EHR or Teams.

## 5. Data classification

| Class | Examples | Handling |
|---|---|---|
| Restricted | PHI, SSNs, payment card data, credentials, security incident details | Encrypt in transit and at rest; minimum necessary access; never email outside Northwind without secure email (type [secure] in the subject) |
| Confidential | Salary data, contracts, financial results, strategy documents | Share only with people who need it; no public links |
| Internal | Policies, procedures, org charts | OK to share inside Northwind |
| Public | Website content, press releases, job postings | No restrictions |

When in doubt, treat information as Restricted.

## 6. Email and phishing

- Use the "Report Phish" button in Outlook for anything suspicious. Do not forward it to coworkers to ask "is this real?"
- External emails are tagged with an [EXTERNAL] banner. Be extra careful with any external email that asks you to log in, pay an invoice, change direct deposit, or open an attachment.
- Requests to change payroll or vendor bank details must always be verified by phone using a number you already have, not one in the email.
- We run monthly phishing simulations. If you click, you will be enrolled in a short training module. Repeated clicks are reported to your manager.

## 7. Access to patient records

Access to the EHR is role-based and follows the minimum necessary standard. Only open records you need for your job. The Privacy Office reviews access logs every day using automated tools that flag, for example, coworker charts, VIP charts, and records with the same last name as the user.

## 8. Using AI tools and cloud services

- Do not paste PHI, patient identifiers, or confidential company data into public AI chatbots or unapproved websites.
- The approved AI assistant for Northwind staff is available through the intranet and does not retain prompts. It is approved for internal and confidential data, but not for PHI unless your use case has been reviewed by the Privacy Office.
- Only use cloud storage and SaaS tools approved by IT (OneDrive, SharePoint, and the tools listed on the "Approved Apps" page). Using an unapproved service for work data is called "shadow IT" and is prohibited.

## 9. Remote and public networks

- Corporate laptops connect through the always-on VPN (GlobalProtect) when off the corporate network.
- Avoid public Wi-Fi for clinical work. If you must use it, the VPN must be connected.
- Home routers should use WPA2 or WPA3 and a non-default admin password.

## 10. Physical security

- Wear your badge. Challenge (politely) anyone without a badge in staff-only areas, or call security.
- Do not let people tailgate behind you through badge doors.
- Print PHI only when necessary and pick it up immediately. Dispose of paper PHI in the locked shred bins.
- Clinic server closets must stay locked. Report a propped-open closet door to the clinic manager.

## 11. Reporting security incidents

Report suspected security incidents immediately - within 1 hour of discovery at the latest. Examples:

* You clicked a link and entered your password
* A lost or stolen laptop, phone or badge
* PHI sent to the wrong person or fax number
* Unexpected MFA prompts
* Unusual behavior on your computer

How to report: call the Service Desk at ext. 4357 (24/7) or email security@northwindhealth.example. You will not be disciplined for honestly reporting your own mistake. You can be disciplined for hiding one.

## 12. Vendors and third parties

Any vendor that will access PHI must sign a Business Associate Agreement (BAA) and pass a security review by the InfoSec team before contract signature. Contact security-reviews@northwindhealth.example at least 30 days before the planned go-live.

## 13. Exceptions

Exceptions to this policy require written approval from the CISO, are time-limited (maximum 12 months), and are tracked in the risk register.

## 14. Training

All workforce members complete information security awareness training at hire and every year after that, as part of annual compliance training.

---
Questions about this policy: security@northwindhealth.example
