# Within a Month — Legal & Compliance Foundations

Once the company exists and you have insurance, lock down the legal and data protection side.

---

## 1. Data Processing Agreement (DPA)

- [ ] Draft a DPA between your company and the tenant
- [ ] Must cover: what personal data you process, why, how long you keep it, security measures, breach notification (72h), sub-processors (AWS)
- [ ] The ICO has template clauses: https://ico.org.uk/for-organisations/
- [ ] Get it signed alongside the subscription agreement

**Why:** UK GDPR requires this whenever you process personal data on behalf of another controller. Without it, both you and your client are non-compliant.

---

## 2. Privacy Policy (for the dashboard)

- [ ] Add a privacy policy page accessible from the login screen
- [ ] Cover: what data you collect (names, emails, IP addresses, images), lawful basis, retention periods, user rights (access, deletion, portability), your ICO registration number
- [ ] Keep it plain English, not legalese

**Why:** Legal requirement for any service collecting personal data. Also signals to your client's users that their data is handled properly.

---

## 3. Terms of Service

- [ ] Basic ToS for dashboard users covering: acceptable use, account responsibilities, intellectual property, limitation of liability
- [ ] Doesn't need to be long — 1–2 pages is fine at this stage

**Why:** Protects you if a user does something unexpected with the platform.

---

## 4. Incident Response Plan

- [ ] Document a simple process:
  - How you detect a breach or data loss
  - Who you notify (client within 24h, ICO within 72h if personal data affected)
  - Steps to contain and remediate
  - Post-incident review
- [ ] Store it in `requirements/` or a dedicated `operations/` folder

**Why:** GDPR requires you to report breaches within 72 hours. Having a plan means you don't panic and miss the deadline. Also reassures clients during due diligence.

---

## 5. Proper Invoicing

- [ ] Set up FreeAgent, Xero, or similar (FreeAgent is popular with UK Ltd companies, integrates with banks)
- [ ] Issue monthly invoices with: company name, company number, registered address, payment terms (14 or 30 days), line items
- [ ] Set up a standing payment or Direct Debit if the client prefers

**Why:** Professional invoices with company details are a legal requirement for Ltd companies. Also makes your accountant's life easier at year-end.
