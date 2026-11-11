BSA/AML & KYC GUIDE FOR LUMEN STAFF
===================================

Owner: Compliance (BSA Officer: Gideon Achterberg) · Sponsor: Adaeze Okafor, Chief Risk Officer
last updated 2026/08/04 · internal, all staff

---

## Why every employee needs to read this

Lumen is not a bank, but we move money for merchants and we service loans issued by Harbor Plains Bank. Under our program agreement with the bank, and as a registered money services business, we must run a Bank Secrecy Act / Anti-Money Laundering (BSA/AML) program. That program only works if people in Sales, Support, Engineering and Lending notice things and say something.

Failures here are not "just compliance problems." They can lead to fines, loss of our bank partner, and card network penalties. In the worst case, they mean we helped someone launder money or fund something terrible.

## 1. Key terms (plain English)

- **KYC / KYB** — Know Your Customer / Know Your Business. Verifying who a merchant is and who owns it.
- **Beneficial owner** — any person who owns 25% or more of the business, plus one person with significant control (e.g. CEO, managing member).
- **CDD / EDD** — Customer Due Diligence (standard checks) and Enhanced Due Diligence (deeper review for higher risk merchants).
- **SAR** — Suspicious Activity Report, filed with FinCEN. Only Compliance files SARs.
- **OFAC** — the U.S. Treasury office that publishes sanctions lists. We can't do business with sanctioned people or countries.

## 2. Merchant onboarding checks

Every new merchant goes through these steps in **Keystone**, our onboarding system:

1. Business verification — legal name, EIN, address, and Secretary of State record.
2. Beneficial owner identification — name, date of birth, address, SSN or ITIN, and a government ID photo with selfie liveness check, via our identity vendor.
3. Sanctions screening — all owners and the business against OFAC SDN and other lists.
4. Website and business-model review — does the business actually do what it says? Is it on the prohibited list?
5. Risk rating — Keystone assigns **Low, Medium or High** based on industry, expected volume, geography and ownership structure.

About 82% of applications clear automatically. The rest go to the Onboarding Review queue, staffed in Austin and Denver. The target is to decide queued applications within one business day.

### 2.1 Prohibited and restricted businesses

| Category | Status |
|---|---|
| Cannabis (including CBD with >0.3% THC), firearms sales, adult content, online gambling, crypto exchanges, money transmitters | **Prohibited** |
| Pawn shops, check cashers, travel agencies, nutraceuticals, ticket resellers, used car dealers | Restricted — EDD required, Compliance approval |
| Nonprofits, charities | Allowed, EDD if receiving funds from abroad |

Don't promise a prospect "we can probably make an exception." We generally can't, and it creates risk if a salesperson implies otherwise.

### 2.2 Enhanced Due Diligence

High-risk merchants get EDD: proof of address for each owner, bank statements for the last 3 months, source-of-funds explanation, and a recorded call with the owner. EDD merchants are reviewed again **every 12 months** (Medium: every 24 months, Low: every 36 months).

## 3. Ongoing monitoring

Beacon (our risk engine) and the Compliance monitoring rules in **Sentinel-TM** scan transactions daily. Common alerts:

* Volume far above what the merchant told us at onboarding (e.g. a hair salon processing $400,000 in a month).
* Lots of round-dollar transactions, or many transactions just under $3,000 or $10,000.
* Card testing patterns — hundreds of tiny authorizations.
* Refunds sent to cards that never made a purchase.
* Loan proceeds moved out immediately to unrelated accounts.
* An owner who matches a sanctions or adverse-media hit after onboarding.

Alerts go to the AML Investigations team. Analysts must disposition an alert within 30 days; if it becomes a case, a SAR decision must be made within 30 days of determining the activity is suspicious.

## 4. What you should do if something looks wrong

Anyone can — and should — refer unusual activity. Use the **"Refer to Compliance"** button in Lumen Admin (for any merchant record) or email aml-referrals@lumenfinance.example. Include the merchant ID and what you noticed. That's it. You don't need to be sure.

Things that are worth a referral:

1. A merchant asks how to keep transactions "under the reporting limit".
2. Someone other than the owner is clearly running the account, or ownership changes suddenly.
3. A borrower refuses to explain what the loan is for, or gives a story that doesn't match their business.
4. A caller pressures Support to send a payout to a new bank account urgently.
5. The business address is a mailbox store and the website was created last week.

## 5. Tipping off — the big rule

**Never tell a merchant, borrower or anyone outside Compliance that a SAR has been or may be filed, or that they are under investigation.** This is a federal law, not just a policy. If a merchant asks why their payout is on hold, Support uses the approved script: "Your account is under a routine review. We'll contact you if we need any information." Don't add to it, don't speculate.

Even inside Lumen, SAR information is only shared on a need-to-know basis. Don't discuss specific cases in open Slack channels.

## 6. Sanctions

If Keystone or Sentinel-TM returns a potential sanctions match, the account is frozen automatically until Compliance clears it. Don't release a frozen account or payout yourself — even if the merchant insists it's a false positive (most are, but Compliance must document why). True matches are blocked and reported to OFAC within 10 business days.

## 7. Recordkeeping

We keep KYC documents, transaction records, and investigation files for **at least five years** after the account closes. Records live in the KYC vault (Restricted data — see the IT Security Policy). Don't download KYC documents to your laptop, and never attach them to Jira, Slack or email.

## 8. Training

- All employees: BSA/AML & Sanctions Basics in LearnUpon within the first week, then annually.
- Merchant Support, Onboarding Review, Lending Operations, Sales: an additional role-based module (90 min) each year.
- AML Investigations: 16 hours of specialized training per year.

Completion is reported to the board's Risk Committee and to Harbor Plains Bank every quarter. Overdue training blocks your access to Lumen Admin after 30 days.

## 9. Independent testing

The AML program is independently tested every year. The 2026 test is scheduled for October. Findings and remediation plans go to the Chief Risk Officer and the bank.

## 10. Contacts

- BSA Officer: Gideon Achterberg
- AML Investigations manager: Renée Castellano
- Sanctions questions: #compliance-help (no merchant details in the channel, please — DM a team member)

*If you remember one thing: when something feels off, refer it. Compliance would much rather close a hundred harmless referrals than miss one real one.*
