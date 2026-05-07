# Product Roadmap

This document sequences features across phases so MVP scope stays clean and every future feature has a home. Each phase names an exit criterion — we only move forward when that criterion is met.

Features live in one of four states:

- **Committed** — defined, scheduled, ready to build.
- **Planned** — defined, next phase up, no build yet.
- **Under review** — interesting, not yet decided. Needs a separate scope-and-cost spike before committing.
- **Rejected** — considered and declined. Kept here so we don't keep re-litigating.

---

## Phase 0 — MVP (Committed)

**Goal:** a tenant can install the hardware, push hourly snapshots, and log into the dashboard to view them. Admins can triage camera health via Slack.

See existing requirements docs for full specs:

- Hardware deployment — `physical_setup.md`
- Edge-to-cloud ingest with multi-camera support — `software_logic.md`, `api_contract.md`
- Cognito multi-tenant auth (three roles) — `multi-tenant-auth.md`
- Dashboard: site view, per-camera gallery, lightbox, heartbeat — `dashboard.md`
- Flagged cameras (user-raised + auto stale-image) with Slack notifications — `software_logic.md` §6, `dashboard.md` §7–8

**Explicitly NOT in Phase 0:**
- Timelapse generator (contract is drafted in `api_contract.md` so the future surface is stable, but the render Lambda is not built)
- Weather data
- Annotations
- Share links
- Reports
- Any AI

**Exit criterion:** one real tenant with at least one multi-camera site is ingesting snapshots, viewing the dashboard, and has raised and resolved at least one flag.

---

## Phase 1 — Polish & Core Extensions (Planned)

**Goal:** deliver the features most users will notice within the first month of using the MVP. All are additive and low-risk.

### 1.1 Timelapse Generator (promoted from future)
Already specified in `api_contract.md` — `POST /timelapses` — and `dashboard.md` §6. Phase 1 builds the FFmpeg render Lambda, the polling flow, and the generator UI with dual-mode exclusions (range + per-image).

### 1.2 Mobile-Responsive PWA
- Tailwind breakpoints and tested layouts for phones/tablets as first-class targets, not an afterthought.
- Installable PWA manifest and service worker.
- Offline cache of the **latest snapshot per accessible camera** — the most common on-site use case is "check the current view" from the foreman's phone.
- Push notifications for flag updates (optional, deferred if it complicates auth).

### 1.3 Before/After Comparison Slider
- New dashboard route: `/compare?site_id=X&camera_id=Y&a=<timestamp>&b=<timestamp>`.
- Draggable slider reveals image A on the left, image B on the right.
- Both timestamps picked from the same camera's gallery.
- Pure frontend — no new API endpoints. Uses existing presigned URLs.

### 1.4 Bulk Image Export
- New endpoint `POST /exports` accepts a `site_id`, `camera_id`, and date range. Returns an `export_id` immediately.
- Background Lambda streams the matching S3 objects into a ZIP in a staging bucket.
- `GET /exports/{export_id}` returns a 24-hour presigned URL when ready.
- Available to all users who can view the camera. Logged in an audit table (tenant admins can see who exported what).
- Soft cap at 5000 images per export (~2.5 GB). Larger exports split into multi-part downloads.

**Exit criterion:** a user on a phone can view their sites, produce a timelapse, do an A/B compare, and download a month of images as a ZIP.

---

## Phase 2 — Industry Differentiators (Planned)

**Goal:** features that materially change the sales pitch to UK construction firms. Each one directly answers a buyer question ("can you show me the weather on the day the slab was poured?", "can I send this to my insurer?", "can you email me a weekly update automatically?").

### 2.1 Weather Correlation (UK — Met Office)

**Data source:** Met Office DataHub (formerly DataPoint) — official UK weather data. Free tier is generous for hourly observations against fixed locations.

**Site configuration:**
- Each `SITE#` record gains a `latitude` and `longitude` attribute, set during provisioning.
- If not set, weather features are disabled for that site (graceful degradation).

**Ingestion:**
- A scheduled Lambda runs hourly (aligned with the snapshot cadence). For each site, it pulls the most recent observation from the nearest Met Office station: temperature, rainfall (mm), wind speed (m/s), wind direction, cloud cover, and a synoptic condition code.
- Stored in a new DynamoDB item: `PK = TENANT#<tenant_id>`, `SK = WEATHER#<site_id>#<timestamp>`.

**Image enrichment:**
- On ingest, after writing the JPEG to S3, the ingest Lambda looks up the most recent weather record for that site (within the last 90 minutes) and writes a small `.json` sidecar object next to the image. This keeps weather data tied to the image without modifying the JPEG.

**Dashboard UX:**
- Hero tile and lightbox display the weather summary for that snapshot.
- Gallery timeline gets a weather strip along the bottom (icons + rainfall bars).
- CSV export of "images with weather" for a date range — this is the artefact that wins disputes.

**Contract / legal value:** wet-weather extension-of-time claims are one of the top dispute categories in UK construction. A signed image + weather record is powerful evidence. This feature also seeds Phase 3's Dispute Mode.

### 2.2 Image Annotations

- Any authenticated user who can view a camera can open an image in the lightbox and **annotate** it: draw rectangles/arrows/freehand on a transparent overlay, add a short text note.
- Annotations are stored as JSON (SVG-like primitives) in DynamoDB, keyed to the exact snapshot timestamp + camera. Never baked into the JPEG.
- Each annotation records `author_name`, `author_sub` (Cognito user ID), and `created_at`.
- The lightbox shows all annotations for an image, with per-annotation author/timestamp. Authors can edit/delete their own; tenant admins can delete any.
- Annotations export with the image in bulk exports (rendered into a second JPEG or a separate SVG).

### 2.3 Public Share Links (with named-guest annotations)

- Tenant admins generate a share link from any site, camera, or specific date range.
- Options per link: expiry (default 7 days, max 90), allowed scope (latest only / date range / single image), optional password.
- The link opens a **no-login viewer** — a stripped-down dashboard with just the shared content.
- **Named guest flow:** on first open, the viewer is asked to enter their name (and optionally email) before they see anything. Stored as a cookie + audit record.
- **Guest annotations:** while viewing, guests can add annotations (same annotation system as 2.2) — they are attributed to the guest's entered name with `author_sub = "guest:<share_id>:<guest_id>"`. This is brilliant for insurers, inspectors, council officers who need to point at something in the image without getting an account.
- Guest annotations surface in the normal dashboard with a "guest" badge so the tenant team knows the source.
- Each share link has an access log (who opened it, when, from where) visible to the tenant admin.

**Security notes:**
- Share links are validated by a new Lambda that generates short-lived presigned URLs on demand, never exposes the raw S3 structure, and enforces the link's scope.
- Revocable from the admin UI at any time.
- Rate-limited per IP to deter scraping.

### 2.4 Scheduled Progress Reports (admin-configured notification cycles)

A dedicated admin panel for setting up recurring reports.

**Per-cycle configuration (stored in DynamoDB as `TENANT#<tenant>` / `REPORT#<report_id>`):**
- Name (e.g., "Weekly site 001 update").
- Scope: site(s) + camera(s) to include.
- Cadence: daily / weekly / monthly. Day of week/month and time (tenant timezone).
- Recipients: list of email addresses (not required to be dashboard users).
- Content toggles: hero snapshots, weather summary, flag events, milestone log entries, before/after from start of period.
- Output format: PDF attachment, inline HTML email, or both.

**Rendering pipeline:**
- EventBridge scheduled rule fires the report-render Lambda at the configured time.
- Lambda assembles the images, weather, and flag data, renders a PDF (HTML → PDF via a headless Chromium layer), uploads to S3, and emails the recipients via SES.
- Failures retry once, then alert the configuring admin.

**Access control:** only tenant admins and super admins can create, edit, or delete report schedules. Any user can view the resulting PDFs in a "Reports" archive tab (scoped by their site access).

**Exit criterion:** a tenant admin can configure a weekly report with weather and flags, and recipients (some non-users) receive a usable PDF every Monday at 8am.

---

## Phase 3 — Advanced UX (Planned, lower priority)

### 3.1 Milestone / Event Log
Lightweight structured timeline per site: `{ timestamp, label, note, author }`. Events pin to the gallery timeline, appear in scheduled reports, and filterable in the gallery view. Useful labels: "slab poured", "topping out", "weather-tight", "handover". No free-text chaos — tenant admin defines the allowed labels per tenant.

### 3.2 Drift Overlay (temporal ghost)
Toggle on the hero/focused view. Picks the same-time-of-day image from N days/weeks/months ago and overlays it at 40% opacity. Zero ML, high wow factor, genuinely useful for spotting settlement, misalignment, and slow-moving changes the eye otherwise misses. UI-only feature — no new endpoints, just a date picker and an opacity slider.

### 3.3 Public Project Page per Site
Branded, read-only page for a site — hero snapshot, progress bar (tied to milestone log), last-updated indicator. Tenant admins toggle on/off per site and choose branding (logo, colors). Designed for principals to embed in investor decks, council updates, and community comms. Does NOT reuse the share-link system — this is a long-lived, curated marketing page with stable URL.

**Exit criterion:** at least one tenant is running a live public project page linked from their own website.

---

## Phase 4 — AI (Under Review)

AI features require model selection, accuracy validation on real UK construction imagery, and cost analysis (per-image inference ≠ free). Each item below is **not committed** — each needs a scoping spike before we promise it to customers.

### 4.1 Automatic Camera Health Detection
Vision model on ingest detects: obstruction (webs, mud, dust), severe blur, exposure failure, orientation drift (drooped mount). High-confidence detections auto-raise the relevant flag reason, and propose timelapse exclusion ranges in the generator UI. Closes the loop on the existing flag pipeline — no new surface area for the user. This is the **lowest-risk AI feature** and the one I'd pilot first.

### 4.2 PPE / Safety Compliance Detection
Hard hat, hi-vis, exclusion-zone presence. UK HSE reporting and insurer requirements make this a premium-tier upsell. Requires a model trained on construction PPE (off-the-shelf models exist, accuracy varies). New safety dashboard + alert thresholds ("≥3 non-compliant frames in a day → flag").

### 4.3 Progress Milestone Auto-Detection
Classifier detects structural phase (excavation → formwork → pour → frame → clad → roof). Auto-populates the milestone log. Best if fed the project's BIM/plan; without that it's a confidence-scored suggestion the tenant admin confirms.

### 4.4 Natural-Language Archive Search
"Find images with a crane", "when did they pour the east slab", "first clear day in June". CLIP-style embeddings indexed in pgvector or OpenSearch. High perceived value, moderate build cost. Pairs well with 4.3.

### 4.5 AI-Written Report Summaries
Feed the week's snapshots + flags + weather + milestones to an LLM with a fixed template; produces a two-paragraph narrative for the Phase 2.4 scheduled reports. Cheap to prototype once scheduled reports exist.

**Phase 4 gate:** before any AI work starts, we need (1) a cost model per tenant, (2) a decision on self-hosted vs. API (Bedrock, OpenAI), (3) a consent/privacy check — especially for PPE detection, which touches GDPR concerns around worker identification.

---

## Backlog — Under Review (not phased yet)

These are interesting but not yet assigned to a phase. Revisit before Phase 2 is complete.

### B.1 Dispute Mode (tamper-evident image packets)
One-click generation of a signed packet for a date range: images + weather + flag history + exclusion audit + chain-of-custody. Every image gets a cryptographic hash recorded at ingest time, and a Merkle root is maintained per site/day, so any alteration is detectable.

**Phase 0 foundation already in place:** per-image SHA-256 hashes are computed and stored in DynamoDB and S3 metadata on every ingest (see `software_logic.md` §8). The packet UI, Merkle roots, signed manifests, and verification endpoint are the remaining B.1 work — none of it requires a backfill.

### B.2 Time-of-Day Normalized Timelapse
Generator option: instead of "every frame in range", pick the single best-lit frame per day (nearest solar noon, cloud-weighted). Result is dramatically smoother. Small engineering effort, big perceived quality win. Requires solar position math (cheap — pyephem or similar) and optional brightness histogram check. Note for Phase 1 or early Phase 2.

---

## Rejected (for now)

Features I considered but declined, with reasoning so they don't get re-raised:

- **After-hours security anomaly detection** — hourly snapshots are too sparse for useful security monitoring. Revisit only if someone asks for sub-10-minute ingest cadence.
- **Unified ingest for drone / body-worn / GoPro footage** — strategically attractive (becomes "the construction visual record system" rather than "the pole camera viewer") but scope-expanding at a stage where we should focus on making pole cameras excellent.

---

## Review Reminders

Features, parameters, or decisions that are defensible for launch but want a proper second look on a scheduled date.

| Item | Revisit by | Trigger |
| :--- | :--- | :--- |
| Image retention default (5 years, tenant-configurable 1–10) | June 2027 | First images approach their expiry. Confirm that 5y matches the real pattern of dispute / handover timing observed in the field. |
| GDPR worker-image SAR handling | First SAR received | The MVP answer is a manual super-admin export. If SARs happen >2×/year, invest in a proper self-service tool. |
| Camera staleness threshold (24h default) | Phase 2 kickoff | Confirm the threshold after 3 months of real-world ingest data. If most cameras recover within 6h, the 24h default is too slow; if a lot of flapping happens at 24h, too tight. |
| Design system (liquid glass) | Before Phase 2 | Gather reactions from real construction-industry users. If glass feels "fancy but not trustworthy", consider a more conservative treatment for admin surfaces. |

---

## Cross-Phase Concerns

### Phase 0 data-model preparation (COMMITTED)

These are additive to MVP scope so future features are not blocked by expensive backfills:

- **Per-image SHA-256 hash on ingest** — ingest Lambda computes the hash, writes it to S3 object metadata (`x-amz-meta-sha256`) and a new DynamoDB `IMG#<site>#<camera>#<timestamp>` item. Specified in `software_logic.md` §8 and surfaced in the `POST /ingest` response. Inert until Dispute Mode (B.1) consumes it.
- **`latitude`, `longitude`, and `timezone` on `SITE#` records** — required at site provisioning via `POST /sites`. Specified in `multi-tenant-auth.md` §3 and `api_contract.md`. Inert until Phase 2.1 weather correlation consumes them.

### Phase ordering flexibility
Phases are not strictly sequential. Cheap Phase 3 items (e.g., drift overlay, ~a day of work) can jump ahead of expensive Phase 2 items if sales momentum demands it. The phase number is priority, not a release lock.
