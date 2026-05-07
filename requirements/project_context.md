# Project Context: SiteSpy — Edge-to-S3 Timelapse & Remote Monitoring

**SiteSpy** is a multi-tenant construction progress monitoring platform. Pole-mounted Axis P1455-LE cameras on construction sites push hourly snapshots to AWS S3 over Starlink. Stakeholders log into a liquid-glass-styled React dashboard to browse snapshots, generate timelapses, and triage camera health.

## 1. Hardware Stack
- **Camera:** Axis P1455-LE (PoE, Linux-based edge device).
- **Connectivity:** Starlink Satellite Internet (high latency, CGNAT, proprietary router).
- **Local Network:** Unmanaged TP-Link PoE Switch via Starlink Ethernet Adapter.
- **Mounting:** High-altitude pole mount with external IP66 enclosure.

## 2. Infrastructure Constraints
- **Networking (CGNAT):** No public IPv4 address. Direct inbound traffic (port forwarding) is not feasible.
- **Stability:** Starlink is prone to 1–5 second "micro-drops." All edge-to-cloud logic MUST include retries and buffers.
- **Data Model:** The camera pushes a 1080p JPEG snapshot to AWS S3 every 60 minutes.

## 3. Implementation Strategy (The "Bridge")
- **Edge Side:** The Axis built-in Event Engine (VAPIX) triggers an HTTPS POST to the ingest endpoint on a 60-minute schedule.
- **Cloud Side:** AWS SAM (Serverless Application Model).
    - **Endpoint:** API Gateway (REST, public).
    - **Compute:** Lambda (Python 3.12) handles binary image data and writes to S3.
    - **Storage:** S3 bucket with lifecycle policies for long-term timelapse storage.

## 4. Specific Parameters
- **Snapshot Frequency:** Every 60 minutes, per camera.
- **Edge Buffer:** 5-second pre-buffer on the camera to handle network jitter during upload.
- **Ingest Authentication:** HTTP Basic Auth with per-camera credential pair, stored in AWS Secrets Manager. Minted at camera registration, rotatable on demand. See `api_contract.md` for full spec.
- **Camera Identity:** Ingest requests include `?cameraID=<camera_id>` so multi-camera sites can be addressed without extra infrastructure.
- **Timestamp Format:** ISO8601 UTC (`YYYY-MM-DDTHH:mm:ssZ`), used in S3 keys and all API responses.
- **S3 Key Structure:** `<tenant_id>/<site_id>/<camera_id>/<YYYY>/<MM>/<DD>/<YYYY-MM-DDTHH:mm:ssZ>.jpg`
- **AWS Region:** `eu-west-2` (London) for UK data residency.
- **API Versioning:** all routes under `/v1/`, breaking changes go to `/v2/`.
- **Retention:** 5 years default, tenant-configurable 1–10 years.

## 5. Platform Role Model

The system is multi-tenant with three distinct roles, enforced via Cognito groups:

- **Super Admin** — operates the platform, sees every tenant. Primary job: triage cross-tenant camera health via the Flagged Cameras console. Notified in Slack when any flag is raised.
- **Tenant Admin** — operates one construction company. Sees every site in their tenant, assigns users to sites, reviews and resolves flags for their tenant.
- **User** — assigned to specific sites by a tenant admin. Views snapshots from every camera on those sites, raises flags per camera, generates per-camera timelapses.

See `multi-tenant-auth.md` for the full role matrix and authorization rules.

## 6. Key Cross-Cutting Features

- **Multi-Camera Sites.** A site may have one or many cameras. Each camera has a stable `camera_id`, its own S3 prefix, its own snapshot stream, and is independently flaggable. The dashboard renders single-camera sites unchanged and multi-camera sites as a per-camera tile grid.
- **Flagged Cameras.** Any user can flag a specific camera (stale image, physical damage, obstruction, image quality, other). A scheduled Lambda also auto-flags cameras that haven't sent an image in 24 hours. Every flag notifies super admins in Slack and appears in the tenant admin's review console.
- **Timelapse Generation (future).** Users compile a date range into an MP4, one camera per render. Image exclusions (range-based or per-image) let users skip periods where the camera was compromised, e.g., after a mount failure.
