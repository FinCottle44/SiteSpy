# Software Integration & Edge-to-Cloud Logic

## 1. Backend Stack

| Component | Technology | Notes |
| :--- | :--- | :--- |
| IaC | AWS SAM | `template.yaml` defines all resources |
| Runtime | Python 3.12 | All Lambda functions |
| Gateway | API Gateway (REST) | Public endpoint. Basic auth at Lambda layer for ingest, Cognito authorizer for dashboard. All routes live under `/v1/`. |
| Storage | S3 | Lifecycle policies (Standard → Glacier Instant Retrieval at 1 year → expiry at tenant retention) |
| Database | DynamoDB | Site mapping, tenant isolation, flag records, image metadata |
| Identity | Cognito | User pool with groups (`SuperAdmins`, `TenantAdmins`) and custom attributes |
| Secrets | Secrets Manager | Per-camera ingest credentials, Slack webhook URL, third-party API tokens |
| Notifications | Slack Incoming Webhook | Flag alerts (per-flag, immediate) |
| Email | SES (`eu-west-2`) | User invites, scheduled reports (Phase 2) |
| Logging | CloudWatch Logs + Lambda Powertools | Structured JSON, correlation IDs across request lifecycle |
| CI/CD | GitHub Actions | `sam deploy` per environment on push to `main` / `release/*` |

## 2. Edge Configuration (VAPIX / Event Engine)

The Axis camera pushes snapshots autonomously — no external poller. Each camera in a multi-camera site is configured independently with its own `cameraID` query parameter.

### Event Rule Configuration:
- **Condition:** Scheduled Event → every 60 minutes.
- **Action:** Send Image → HTTPS Recipient.
- **Recipient URL:** `https://<api_id>.execute-api.eu-west-2.amazonaws.com/prod/v1/ingest?cameraID=<camera_id>`
- **Auth on recipient:** Basic, with the per-camera username + password minted by `POST /v1/sites/{site_id}/cameras` (see `api_contract.md` and `multi-tenant-auth.md` §5).
- **Custom Headers:**
  - `X-Tenant-ID: <tenant_id>`
  - `X-Site-ID: <site_id>`
  - `Content-Type: image/jpeg`
- **Pre-buffer:** 5 seconds (handles Starlink jitter — camera holds image in RAM and retries).
- **Post-buffer:** 0 seconds.
- **Image Frequency:** 1 frame per trigger.

When adding a second camera at an existing site, register it via `POST /v1/sites/{site_id}/cameras` to mint new credentials, then provision the same recipient URL with a different `cameraID` query value (e.g., `cameraID=cam_02`) and the new Basic Auth pair. No other edge configuration changes.

## 3. Starlink Considerations (CGNAT)

- Starlink does NOT provide a public IP. No inbound connections are possible.
- The camera MUST push outbound. The architecture is designed around this constraint.
- **Remote camera management:** Use Axis Secure Remote Access (built-in TURN/STUN relay) via the Axis Companion app. No VPN or port forwarding required.
- **Micro-drops (1–5 seconds):** The 5-second pre-buffer on the camera handles this. If the POST fails entirely, the image is lost unless MicroSD edge recording is enabled (see below).

## 4. Edge Redundancy (MicroSD)

The camera MUST have a MicroSD card configured as continuous recording storage. If a Starlink outage exceeds the pre-buffer window:
- The image is saved locally on the MicroSD.
- Manual retrieval is possible via Axis Secure Remote Access.
- Automated sync from MicroSD to S3 is NOT in scope for MVP.

## 5. Role-Based Lambda Authorization

Every dashboard-facing Lambda invokes a shared `resolve_role(event)` helper that:
1. Reads `cognito:groups`, `custom:tenant_id`, and `custom:site_access` from the JWT claims (already validated by the Cognito authorizer).
2. Returns one of `super_admin`, `tenant_admin`, `user`.
3. Rejects (`403 ACCESS_DENIED`) any caller in more than one role group.

Each endpoint then calls a `check_access(role, tenant_id, site_id)` helper that applies the role matrix from `multi-tenant-auth.md` Section 4. This keeps authorization in one place — no ad-hoc claim checks scattered across handlers.

## 6. Flag Pipeline

### 6.1 Data Storage

Flags share the Site Mapping DynamoDB table to keep a single data plane. See `api_contract.md` — Flagged Cameras section — for the key schema, the `GSI1` index used for cross-tenant super-admin queries, and the full attribute list.

### 6.2 User-Raised Flags

`POST /flags` writes the record (with a `ConditionExpression` that blocks duplicate open/acknowledged flags on the same camera + reason), then fires the Slack notification. The write and the notification are in the same Lambda; failure to post to Slack does NOT fail the API call — the flag is still persisted, and the Slack failure is logged to CloudWatch with an alarm attached.

### 6.3 Auto-Raised Stale-Image Flags

A scheduled Lambda (`stale-image-detector`) runs every hour via EventBridge:

1. Iterates every `TENANT#*` → `SITE#*#CAM#*` record in the Site Mapping table — one iteration per camera, not per site.
2. For each camera, checks the latest object under `<tenant_id>/<site_id>/<camera_id>/` in S3.
3. If the latest object's timestamp is older than a threshold (default 24 hours, configurable per tenant via a `stale_threshold_hours` attribute on the `TENANT#` record), open an auto-flag with `reason: "stale_image"`, `source: "auto"`, `raised_by: "system"`, scoped to that specific `camera_id`.
4. The same duplicate-suppression logic applies — if an open stale-image flag already exists for this camera, do nothing.

On successful `/ingest`, the ingest Lambda checks for and resolves any open `stale_image` auto-flag for **that specific camera** (not the whole site) with `resolved_by: "system"` and a note "Camera resumed sending images." This keeps the flag log self-healing at camera granularity — one broken camera at a multi-camera site will not mask the recovery of a working one.

### 6.4 Slack Notification

Each flag (user-raised or auto) posts a message to a single Slack Incoming Webhook configured in AWS Secrets Manager (`slack/flag-webhook`).

Message format:
```
🚩 Camera flagged — <tenant_name> / <site_name> / <camera_name> (<camera_id>)
Reason: <reason>
Raised by: <user email or "System">
Note: <note or "—">
Open in console: <deep link to /flags/<flag_id>>
```

The webhook URL is loaded once per Lambda cold start and cached. Notifications go out per-flag, immediately. Batched digests are out of scope for MVP.

### 6.5 Status Transition Audit

`PATCH /flags/{flag_id}` appends a transition record to the flag's attributes (`acknowledged_by`/`acknowledged_at`/`resolved_by`/`resolved_at`, plus `admin_notes`). The Lambda validates the transition against the allowed lifecycle (`open → acknowledged → resolved`, anything → `dismissed`) and returns `409 CONFLICT` on invalid attempts.

## 7. Timelapse Exclusion Pipeline (Future — not MVP)

When `POST /timelapses` arrives with an `exclusions` array, the render Lambda:

1. Resolves the full candidate list of S3 keys in the `from`/`to` range.
2. Filters out any key whose timestamp falls inside an exclusion window or matches a listed timestamp exactly.
3. Runs FFmpeg against the filtered list.
4. Persists the submitted exclusions (with `reason`, `raised_by`, and `raised_at`) as a nested attribute on the timelapse record. Excluded images in S3 are not modified, tagged, or deleted.

Exclusions are one-shot by design — see `dashboard.md` Section 6.2 for the rationale. The DynamoDB record is the audit trail.

## 8. Image Integrity & Hashing (Phase 0 foundation for Dispute Mode)

Every successful `/ingest` computes and stores a SHA-256 hash of the JPEG bytes before responding. This is a foundational investment for the tamper-evident **Dispute Mode** feature listed in `roadmap.md` backlog item B.1. Without it, we'd need to rehash the entire image archive later to enable that feature — prohibitively expensive.

### What the ingest Lambda does (Phase 0):
1. Read the raw JPEG bytes from the request body.
2. Validate tenant/site/camera against the Site Mapping table (unchanged behavior).
3. Compute `sha256 = hashlib.sha256(body).hexdigest()`.
4. `PutObject` to S3 with `Metadata = { "sha256": <hash>, "ingested-at": <ISO8601> }`.
5. `PutItem` to DynamoDB under `PK = TENANT#<tenant>`, `SK = IMG#<site>#<camera>#<timestamp>`, with attributes `s3_key`, `sha256`, `size_bytes`, `ingested_at`, `content_type`.
6. Run the stale-flag resolution (Section 6).
7. Return `201` with the hash included in the response body.

### What Phase 0 does NOT do:
- No Merkle tree, no daily roots, no signed manifests. Those are B.1 work.
- No API exposes the hash to dashboard users beyond the ingest response. The record is passive data.
- No verification endpoint. Verification tooling lives with Dispute Mode.

### Why store in both S3 metadata AND DynamoDB:
- S3 metadata: survives object copies, verifiable without an API call, tamper-resistant if versioning is on.
- DynamoDB: queryable by range, lets B.1 build the Merkle tree efficiently without listing S3.

Storage cost is negligible (~100 bytes per image). Compute cost is a single streaming hash, measured in microseconds for a 500 KB JPEG.

## 9. Seeding & Environment Bootstrap

### 9.1 First super admin

Cognito is empty on a fresh stack deploy. The SAM template accepts a `BootstrapSuperAdminEmail` parameter. On initial deploy (and only when no user with the `SuperAdmins` group exists):

1. A one-time custom resource Lambda creates a Cognito user with the supplied email, adds them to the `SuperAdmins` group, and triggers Cognito's invitation email.
2. The Lambda then disables itself so re-runs are no-ops.

In dev environments, the bootstrap email defaults to a developer's address. In prod, it is the SiteSpy ops contact.

### 9.2 Seed script (`scripts/seed.py`)

A single Python script handles all non-production seeding and also serves as the documented happy-path for provisioning the first real tenant in prod. It uses the AWS SDK directly — not the API — so it is idempotent and survives partial failures.

Commands:

```bash
# Create a tenant + tenant admin user + initial site + first camera, end to end.
python scripts/seed.py bootstrap-tenant \
  --tenant-id acme_corp \
  --tenant-name "Acme Construction Ltd" \
  --admin-email ops@acme.example.com \
  --site-id site_001 \
  --site-name "Acme Tower Phase 2" \
  --latitude 51.5074 --longitude -0.1278 \
  --camera-id cam_01 --camera-name "North elevation"
```

Output:
- Tenant record in DynamoDB.
- Tenant admin user in Cognito (invite email sent).
- Site record in DynamoDB.
- Camera record in DynamoDB plus a fresh credential pair in Secrets Manager.
- Axis VAPIX configuration summary printed to stdout: URL, Basic Auth username, password, required headers. Copy-paste into the camera's web UI during hardware commissioning.

In `dev` the script can also seed a deterministic demo dataset (a few fake snapshots written to LocalStack S3 with realistic timestamps) for frontend development without waiting for real cameras.

### 9.3 Teardown

`scripts/teardown.py --tenant-id <id>` removes a tenant's DynamoDB records, Cognito users, and Secrets Manager entries. Does NOT touch S3 images — deletion there is handled by the purge Lambda driven by the tenant's retention policy. Prod teardowns require `--confirm <tenant-id>`.

## 10. Observability

### 10.1 Structured logging
Every Lambda uses **AWS Lambda Powertools for Python** (`aws-lambda-powertools`). Logs are JSON with at minimum: `correlation_id`, `tenant_id`, `site_id` (when applicable), `user_sub` or `camera_id`, `route`, `status_code`, and `latency_ms`. Correlation IDs propagate via `X-Correlation-Id` header (generated if absent).

### 10.2 Metrics
Custom CloudWatch metrics (EMF via Powertools) per Lambda:
- `IngestSuccess` / `IngestFailure` (dimensions: `tenant_id`, `site_id`, `camera_id`).
- `FlagRaised` (dimensions: `tenant_id`, `reason`, `source`).
- `SnapshotListLatency` (p50/p95/p99).
- `SlackWebhookFailure`.
- `AuthDenied` (`role`, `endpoint`).

### 10.3 Alarms

| Alarm | Threshold | Action |
| :--- | :--- | :--- |
| `IngestFailureRate` | >5% over 15 min | Page ops (SNS → Slack #sitespy-ops + PagerDuty) |
| `IngestCredentialFailures` | >20/min | Slack + capture source IPs for investigation (brute-force detection) |
| `ApiErrorRate5xx` | >1% over 5 min | Slack warning |
| `LambdaThrottle` | any | Slack warning |
| `SlackWebhookFailure` | >0 over 10 min | Slack warning (self-referential: falls back to email if webhook itself is the problem) |
| `DynamoDBThrottle` | any | Slack warning |
| `StaleImageDetectorFailure` | invocation error | Slack warning |
| `UnusualCognitoAdminActivity` | any admin action outside 0800–1800 UK | Slack + audit email |
| `BreachCandidate` | any `AccessDenied` from IAM on S3/DynamoDB outside SAM deploy context | Page ops (this feeds GDPR breach detection) |

### 10.4 Dashboards
CloudWatch dashboard `sitespy-<env>-overview` with tiles for ingest volume per tenant, API latency, error rates, and open flag counts. Visible to ops only, not to tenants.

### 10.5 Tracing
X-Ray enabled on all Lambdas. Traces are sampled at 10% in prod, 100% in dev.

## 11. S3 Lifecycle Policy

Applied to the `project-snapshots` bucket. Storage-class transitions are cost-optimized for "often-accessed this month, occasionally-accessed forever":

| Age | Class | Rationale |
| :--- | :--- | :--- |
| 0 — 365 days | S3 Standard | Dashboard browsing, timelapse generation, active disputes |
| 365+ days | S3 Glacier Instant Retrieval | Still millisecond retrieval, ~68% cheaper than Standard, no retrieval fees for the occasional dispute pull |
| At retention expiry (default 5 years, tenant-configurable) | Deleted | Enforces the documented retention policy |

Lifecycle transitions are driven by S3 object tags set at ingest time:
- `retention_years=5` (or the tenant's configured value)
- `tenant_id=<tenant>`

Lifecycle rules reference these tags, so per-tenant retention changes take effect without touching individual objects. The `IMG#` DynamoDB records are NOT deleted with the S3 objects — metadata is kept indefinitely for audit.

## 12. Timezone Rendering Rule

**Rule:** every timestamp displayed in the dashboard is rendered in the viewed site's timezone (from `SITE#.timezone`, default `Europe/London`), not in the browser's local timezone. Tooltips show the original UTC value for clarity.

**Why:** a construction project manager in Manchester and an investor in Singapore should see "poured 2025-06-15 14:00 BST" on the same snapshot, not two different local times. The site's timezone is the authoritative one.

Implementation: a `<Timestamp />` component takes an ISO8601 UTC string and the site's timezone, renders using `Intl.DateTimeFormat` with the site zone, and exposes the UTC value on hover. All list views, lightboxes, reports, and exports use this component — no raw `.toLocaleString()` calls anywhere in the codebase.

## 13. Environments & CI/CD

### 13.1 Environments
| Env | Stack name | Domain | Data |
| :--- | :--- | :--- | :--- |
| dev | `sitespy-dev` | `dev.sitespy.io` | Throwaway, seeded demo data |
| prod | `sitespy-prod` | `sitespy.io` | Real tenants |

Parameters differ per-env via `samconfig.toml` sections.

### 13.1.1 AWS Account Strategy

**Launch posture (single account, two stacks):** both `sitespy-dev` and `sitespy-prod` live in the consultancy AWS account during the pre-tenant period. Full IaC isolation via two independent CloudFormation stacks. Sufficient blast-radius control for a zero-tenant product.

**Rule:** no real paying tenant is onboarded into `sitespy-prod` while it runs in the consultancy account. Internal test tenants only.

**Cutover to a dedicated business account** happens before first real tenant onboarding. Because nothing live exists in the current `sitespy-prod`, this is a clean re-deploy, not a migration:

1. Create the new AWS account under the SiteSpy business entity.
2. Create a root IAM user, enable MFA, store recovery codes securely.
3. Provision a deploy IAM role with the minimum policy documented in `scripts/iam/deploy-policy.json`.
4. Update `samconfig.toml` with a `prod` section pointing at the new account's profile.
5. `sam deploy --config-env prod` — creates the stack from scratch.
6. Provision a new `sitespy.io` ACM cert and attach to the new API Gateway custom domain and Amplify Hosting app.
7. Re-seed the super admin via `BootstrapSuperAdminEmail`.
8. Cut over DNS for `sitespy.io` (Route 53 hosted zone in the new account, or delegate from wherever the registrar keeps it).
9. Decommission the old `sitespy-prod` stack in the consultancy account. Leave `sitespy-dev` in place.

No data migration is required **provided** rule above is honored.

### 13.2 Pipeline (GitHub Actions)

- **Pull request:** lint + type-check (backend: `ruff`, `mypy`; frontend: `eslint`, `tsc --noEmit`), run all tests, build SAM package, preview comment with the changed resources.
- **Merge to `main`:** deploy to `dev` automatically.
- **Push to `release/*`:** deploy to `prod` with manual approval on the GitHub Environment gate.
- **Frontend:** Amplify Hosting builds from the same repo on the same branches.

### 13.3 Testing strategy
- **Backend:** `pytest`. Unit tests for the role matrix, schema validation, flag state transitions. Integration tests run against LocalStack for DynamoDB, S3, Cognito.
- **Frontend:** `vitest` + React Testing Library. One Playwright happy-path e2e per role (user browses gallery, tenant admin raises and resolves a flag, super admin provisions a tenant).
- **Coverage target:** 80% on backend, not enforced on frontend (too noisy for UI).

## 14. API Versioning

All routes live under `/v1/` from first commit. Breaking changes land on `/v2/` while `/v1/` continues to serve deployed cameras. Non-breaking additions (new optional fields, new endpoints) go on `/v1/`.

A Lambda layer shared across handlers exposes the versioning contract — adding a field is a one-line change, bumping the version is a deliberate act that requires a parallel handler.

## 15. Deployment Checklist

- [ ] Set camera admin password (complex, stored in Secrets Manager or parameter store).
- [ ] Enable HTTPS-only on the camera (self-signed cert is acceptable for outbound POST).
- [ ] Format MicroSD as "Primary Storage" for local redundancy.
- [ ] Configure NTP on the camera (use `pool.ntp.org` or AWS NTP `169.254.169.123`) — timestamps MUST match UTC ISO8601.
- [ ] Configure VAPIX Event Rule per Section 2 above, using the per-camera Basic Auth pair from `POST /v1/sites/{site_id}/cameras`.
- [ ] Deploy SAM stack (`sam build && sam deploy --guided` for the first deploy, `sam deploy` thereafter). Supply `BootstrapSuperAdminEmail` on the first prod deploy.
- [ ] Confirm the first super admin received the Cognito invitation email and can log in.
- [ ] Create a test tenant using `scripts/seed.py bootstrap-tenant` and confirm the flow end-to-end.
- [ ] Create Cognito groups: `SuperAdmins` and `TenantAdmins` (the SAM template does this, but verify).
- [ ] Seed DynamoDB Site Mapping table with initial tenant/site/camera record(s) via the seed script, including `GSI1` for flag queries. Every `SITE#<site_id>` item MUST include `latitude`, `longitude`, and `timezone`.
- [ ] Confirm DynamoDB capacity planning accounts for the new `IMG#<site>#<camera>#<timestamp>` item written on every ingest (single-table design, no schema change required).
- [ ] Create Slack Incoming Webhook and store the URL in Secrets Manager at `slack/flag-webhook`.
- [ ] Deploy the `stale-image-detector` EventBridge schedule (hourly).
- [ ] Configure CloudWatch alarms per §10.3.
- [ ] Apply the S3 lifecycle policy per §11.
- [ ] Verify end-to-end: trigger a manual snapshot from the camera, confirm it lands in S3 with correct key structure and that the `x-amz-meta-sha256` S3 object metadata is populated.
- [ ] Verify flag flow: raise a test flag via the dashboard, confirm Slack message arrives, acknowledge and resolve from the admin console.
- [ ] Verify CCTV signage is in place at the installed site before go-live (tenant responsibility, super admin confirmation).
