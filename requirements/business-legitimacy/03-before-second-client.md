# Before Second Client — Operational Maturity

Things that don't block your current engagement but should be in place before you scale to a second tenant or start marketing.

---

## 1. SLA Document

- [ ] Formalise your uptime and support commitments:
  - API uptime target (e.g., 99.5% monthly)
  - Image retention guarantee (5 years default, configurable)
  - Support response time (e.g., 1 business day)
  - Maintenance windows (if any)
  - Exclusions (force majeure, client-side network issues)
- [ ] Reference this in the subscription agreement

**Why:** Sets expectations before they become complaints. Also a sales tool — construction firms buying software want to know it's reliable.

---

## 2. Backup & Disaster Recovery Documentation

- [ ] Document what's already in place:
  - S3 versioning and lifecycle rules
  - DynamoDB Point-in-Time Recovery (PITR)
  - Cognito user pool export capability
  - Infrastructure-as-code (SAM template) means full redeploy is possible
- [ ] Document RTO (recovery time objective) and RPO (recovery point objective)
- [ ] Test a restore at least once and record the result

**Why:** Your client may ask "what happens if something goes wrong?" Having a written answer (rather than "trust me") is the difference between a professional service and a side project.

---

## 3. Public Liability Insurance

- [ ] Get a quote if you ever go on-site (camera installs, maintenance visits)
- [ ] Many construction sites require it for any visitor doing work
- [ ] ~£100–300/year for low-risk tech work

**Why:** If you trip over a cable on-site and damage something (or yourself), this covers it. Some sites won't let you through the gate without a certificate.

---

## 4. Landing Page / Marketing Site

- [ ] Proper single-page or multi-page site on your domain
- [ ] Cover: what SiteSpy does, key features, a screenshot or two, contact/demo CTA
- [ ] Doesn't need to be elaborate — Framer, Webflow, or even a static site on Amplify
- [ ] Add a footer with company number, registered address, ICO registration

**Why:** When someone Googles your company name or you send a cold email, there needs to be something credible at the other end.

---

## 5. Standardised Onboarding Process

- [ ] Document the steps to bring a new tenant live:
  - Provision tenant in Cognito
  - Create site records with lat/lng
  - Ship and install hardware
  - Create tenant admin account
  - Send welcome email with credentials and docs
- [ ] Partially automated via your existing seed scripts, but document the human steps too

**Why:** If onboarding is in your head, it doesn't scale. Even for client #2, having a checklist means nothing gets missed.
