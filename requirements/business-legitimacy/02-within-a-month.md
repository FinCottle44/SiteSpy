# Within a Month — Legal & Compliance Foundations

Once the company exists and you have insurance, lock down the legal and data protection side.

---

## 1. Data Processing Agreement (DPA)

- [ ] Draft a DPA between your company and the tenant
- [ ] Must cover: what personal data you process, why, how long you keep it, security measures, breach notification (72h), sub-processors (AWS)
- [ ] Get it signed alongside the subscription agreement

**Where to get it:**
- **ICO** (ico.org.uk/for-organisations) — free controller/processor contract guidance and template clauses. Start here.
- **Docular / SEQ Legal** (docular.net) — free/cheap UK-drafted DPA templates you can adapt.
- **Solicitor** — worth a review if the client is large or pushes back on terms.

**Why:** UK GDPR requires this whenever you process personal data on behalf of another controller. Without it, both you and your client are non-compliant.

**Note:** Can be signed now as a sole trader — this is a live compliance gap with your current client, so don't wait for the Ltd. Reissue under the Ltd when it exists.

---

## 2. Privacy Policy (for the dashboard)

- [ ] Add a privacy policy page accessible from the login screen
- [ ] Cover: what data you collect (names, emails, IP addresses, images), lawful basis, retention periods, user rights (access, deletion, portability), your ICO registration number
- [ ] Keep it plain English, not legalese

**Where to get it:**
- **Termly** (termly.io) or **iubenda** (iubenda.com) — free/low-cost privacy policy generators that produce a GDPR-compliant policy from a questionnaire. Fastest route.
- **Docular / SEQ Legal** (docular.net) — free UK-drafted templates if you'd rather edit a document than use a generator.
- **ICO** (ico.org.uk) — guidance on what a privacy notice must contain (use to sanity-check whatever you generate).

**Why:** Legal requirement for any service collecting personal data. Also signals to your client's users that their data is handled properly.

---

## 3. Terms of Service

- [ ] Basic ToS for dashboard users covering: acceptable use, account responsibilities, intellectual property, limitation of liability
- [ ] Doesn't need to be long — 1–2 pages is fine at this stage

**Where to get it:**
- **Termly / iubenda** — same generators as the privacy policy; they produce a matching ToS.
- **Docular / SEQ Legal** — free "website terms of use" / SaaS terms templates.

**Why:** Protects you if a user does something unexpected with the platform.

---

## 4. Incident Response Plan

- [ ] Document a simple process:
  - How you detect a breach or data loss
  - Who you notify (client within 24h, ICO within 72h if personal data affected)
  - Steps to contain and remediate
  - Post-incident review
- [ ] Store it in `requirements/` or a dedicated `operations/` folder

**Where to get it:**
- **NCSC** (ncsc.gov.uk) — free "Small Business Guide" and incident response templates aimed at exactly your size of org.
- **ICO** (ico.org.uk) — personal data breach guidance and what/when to report.
- This one you can largely write yourself; it's a short process doc, not a legal contract.

**Why:** GDPR requires you to report breaches within 72 hours. Having a plan means you don't panic and miss the deadline. Also reassures clients during due diligence.

---

## 5. Proper Invoicing

- [ ] Set up FreeAgent, Xero, or similar (FreeAgent is popular with UK Ltd companies, integrates with banks)
- [ ] Issue monthly invoices with: company name, company number, registered address, payment terms (14 or 30 days), line items
- [ ] Set up a standing payment or Direct Debit if the client prefers

**Why:** Professional invoices with company details are a legal requirement for Ltd companies. Also makes your accountant's life easier at year-end.
