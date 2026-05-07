# Multi-Tenant Auth & Data Isolation

The system serves multiple construction companies. "Company A" MUST NOT be able to see camera feeds, flags, or timelapses belonging to "Company B."

The camera is an **agent** (pushes data). Dashboard users are **consumers** (view data). The two never communicate directly.

---

## 1. Role Hierarchy

The platform has three distinct roles. Role is determined exclusively by Cognito **group membership** — never by a hand-rolled `custom:role` attribute. Group membership is emitted in the JWT as the `cognito:groups` claim, which API Gateway and Lambda authorizers read directly.

| Role | Cognito Group | `custom:tenant_id` | `custom:site_access` | Scope |
| :--- | :--- | :--- | :--- | :--- |
| Super Admin | `SuperAdmins` | unset / empty | unset / empty | Entire platform, across all tenants. Operates the service. |
| Tenant Admin | `TenantAdmins` | required | ignored (implicit all) | All sites within their tenant. Assigns users to sites. |
| User | (no group) | required | required | Only the sites explicitly listed in `site_access`. |

**Rules:**
- A user MUST belong to at most one role group. Mixed membership is rejected at the authorizer.
- Super admins have no tenant. Any API that requires a `tenant_id` (e.g., cross-tenant flag queries) takes it as a query parameter when called by a super admin.
- Tenant admins do NOT use the `*` wildcard in `site_access`. Their tenant-wide access is granted purely via group membership. This removes the dual-path ambiguity ("is it `*` or is it the group?") from authorization code.

---

## 2. Cognito User Pool

- **Groups:** `SuperAdmins`, `TenantAdmins`. Regular users are in no group.
- **Custom Attributes:**
  - `custom:tenant_id` (String) — the company this user belongs to. Required for tenant admins and users.
  - `custom:site_access` (String) — comma-separated site IDs the user may view. Required for users only.
- **App Client:** Public web client (no client secret) for the React dashboard.
- **Token Usage:** The frontend sends the Cognito ID Token as `Authorization: Bearer <id_token>` on every API call.

### Example token claims — regular user:
```json
{
  "sub": "a1b2c3d4-...",
  "cognito:groups": [],
  "custom:tenant_id": "acme_corp",
  "custom:site_access": "site_001,site_002"
}
```

### Example token claims — tenant admin:
```json
{
  "sub": "e5f6g7h8-...",
  "cognito:groups": ["TenantAdmins"],
  "custom:tenant_id": "acme_corp"
}
```

### Example token claims — super admin:
```json
{
  "sub": "i9j0k1l2-...",
  "cognito:groups": ["SuperAdmins"]
}
```

---

## 3. Site Mapping Table (DynamoDB)

Maps tenants → sites → cameras. A site MAY contain multiple cameras, and every camera is represented as its own item. Site-level metadata lives on a separate item so it isn't duplicated per camera.

| Key | Type | Example | Purpose |
| :--- | :--- | :--- | :--- |
| PK | Partition Key | `TENANT#acme_corp` | — |
| SK | Sort Key | `SITE#site_001` | Site-level metadata item |
| SK | Sort Key | `SITE#site_001#CAM#cam_01` | Camera item |

**Site-level attributes** (`SITE#<site_id>`): `site_name`, `latitude`, `longitude`, `timezone` (IANA, default `Europe/London`), `address` (freeform, optional).

**Camera-level attributes** (`SITE#<site_id>#CAM#<camera_id>`): `camera_name`, `camera_model`, `starlink_status`, `s3_bucket_path`.

**`latitude` and `longitude` are required** at site provisioning. They enable weather correlation in Phase 2 without a backfill. Provisioning UX MUST validate the pair (both present, within valid ranges) before writing the site item. The ingest path does not consume these fields; they are metadata-only in Phase 0.

To list every camera on a site, the API runs a `Query` on PK = `TENANT#<tenant_id>` with the SK prefix `SITE#<site_id>#CAM#`. Access is granted per site, never per camera — if a user can see the site, they can see all its cameras.

Tenant admins MAY write to this table (via API) to assign users to sites and to register new sites and cameras. Regular users and the ingest path are read-only.

---

## 4. Authorization Flow (per API request)

1. API Gateway validates the JWT signature (Cognito authorizer).
2. Lambda extracts `cognito:groups`, `custom:tenant_id`, and `custom:site_access` from the token.
3. Lambda resolves the effective role:
   - `SuperAdmins` group → `super_admin`
   - `TenantAdmins` group → `tenant_admin`
   - otherwise → `user`
4. Lambda applies role-based access:
   - **Super admin:** allowed on any tenant/site. If the endpoint requires a `tenant_id`, it MUST be supplied as a query parameter.
   - **Tenant admin:** `requested.tenant_id == token.tenant_id`. Site ID is not checked against an allow-list (all sites in the tenant are permitted). The `TENANT#<tenant_id> / SITE#<site_id>` record MUST exist in the Site Mapping table.
   - **User:** `requested.tenant_id == token.tenant_id` AND `requested.site_id ∈ token.site_access`. The `TENANT#<tenant_id> / SITE#<site_id>` record MUST exist.
5. If any check fails → `403 ACCESS_DENIED`.
6. On success → Lambda generates short-lived S3 pre-signed URLs (5-minute TTL) where applicable.

---

## 5. Ingest Authentication (Camera → Cloud)

The Axis camera authenticates to `POST /v1/ingest` via **HTTP Basic Authentication** using a per-camera credential pair. Basic auth is required because Axis VAPIX's HTTPS recipient does not support custom headers — it can only supply `Authorization: Basic`.

### Credential lifecycle

1. **Minted** at `POST /v1/sites/{site_id}/cameras` by a tenant admin or super admin. The server generates a cryptographically random username (32 chars, prefix `sitespy_cam_`) and password (48 chars, mixed case + digits). The response contains the pair in plaintext and it is shown **once**.
2. **Stored** in AWS Secrets Manager at `sitespy/cameras/<tenant_id>/<site_id>/<camera_id>`. The secret value binds the credential to its tenant/site/camera. The Lambda service role has read access to `sitespy/cameras/*`.
3. **Presented** by the Axis camera on every ingest POST. The ingest Lambda:
   - Looks up the secret by tenant/site/camera path derived from the request.
   - Constant-time compares the submitted username and password.
   - Verifies the request's `X-Tenant-ID`, `X-Site-ID`, and `cameraID` match the secret's binding (defense in depth).
4. **Rotated** via `POST /v1/sites/{site_id}/cameras/{camera_id}/rotate-credentials`. The old credential is invalidated immediately; the Axis device must be reconfigured before its next snapshot.
5. **Revoked** when the camera is deleted. The secret is scheduled for deletion with a 7-day recovery window.

### Why not a single shared API key?
- Revocation blast radius: one compromised camera would force every camera on the platform to rotate.
- Audit: basic auth credentials are already per-camera, so every log line has the right subject.
- Rate limiting: API Gateway usage plans attach per credential, so per-camera throttling comes for free.

Additional required headers on ingest (unchanged from earlier drafts):
- `X-Tenant-ID: <tenant_id>`
- `X-Site-ID: <site_id>`

Query param: `cameraID=<camera_id>`.

The ingest path is never consumed by human users, so it does not interact with Cognito.

---

## 6. Frontend Auth Implementation (Kiro-Built)

Tech: React 18 + TypeScript + `aws-amplify`.

The frontend MUST:
1. Use Amplify's `signIn` / `signUp` / `signOut` flows against the Cognito User Pool.
2. After login, extract `cognito:groups`, `custom:tenant_id`, and `custom:site_access` from the ID Token and derive the effective role (see Section 4).
3. Render role-appropriate navigation:
   - Super admin → global **Flagged Cameras** console, tenant switcher.
   - Tenant admin → all sites in the tenant, user management, flag review for their tenant.
   - User → assigned sites only, ability to raise flags.
4. Pass the ID Token as `Authorization: Bearer <token>` on all API calls.
5. Display "Access Denied: You do not have access to this site." on any `403` response.
6. Never store or expose the raw JWT in localStorage — use Amplify's built-in secure token management.

### Configuration (environment variables):
```
VITE_USER_POOL_ID=eu-west-2_XXXXXXX
VITE_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxx
VITE_API_ENDPOINT=https://xxxxxxxxxx.execute-api.eu-west-2.amazonaws.com/prod
```

Region is `eu-west-2` (London) for UK data residency. See Section 8 for GDPR scope.

---

## 7. Summary of Responsibilities

| Feature | Backend (AWS Lambda / SAM) | Frontend (React / Kiro-Built) |
| :--- | :--- | :--- |
| Identity | Cognito User Pool + groups + custom attributes | Amplify login/signup flow |
| Role resolution | Lambda authorizer reads `cognito:groups` | Frontend renders role-aware navigation |
| Image Ingest | Lambda validates per-camera basic auth + writes to S3 | N/A (hardware triggered) |
| Data Isolation | DynamoDB partition + group/claim checks | Passes JWT, handles 403 gracefully |
| Image Viewing | Generates pre-signed URLs (5-min TTL) | Gallery, timeline, lightbox UI |
| Flag Review | Role-scoped `/v1/flags` endpoints | Flag console (super admin + tenant admin) |
| User / Tenant / Camera CRUD | `/v1/tenants`, `/v1/users`, `/v1/sites`, `/v1/sites/{id}/cameras` | Admin UIs, onboarding, camera credential display |

---

## 8. Data Residency, Retention & GDPR

### 8.1 Data residency
All personal data and image storage is hosted in AWS region `eu-west-2` (London). No cross-region replication to non-EU/UK regions. Backups (Cognito, DynamoDB PITR, S3 cross-region) stay within UK or EU regions.

### 8.2 Image retention
- **Default retention:** **5 years** from ingest date. After 5 years, images are permanently deleted by an S3 lifecycle policy.
- **Per-tenant override:** the `TENANT#` record may carry a `retention_years` attribute (integer, 1–10). Construction projects with long defect-liability periods commonly want 7–10 years.
- **Review date:** the 5-year default is a launch decision. Revisit before June 2027 when the first images approach deletion, in case long-handover contracts push this higher. Tracked in `roadmap.md` backlog.

### 8.3 Lawful basis and consent
Snapshot images may incidentally contain identifiable workers on site. Lawful basis for processing is **legitimate interest** (construction progress monitoring, safety, dispute evidence) per UK GDPR Article 6(1)(f).

Tenants MUST display a site-notice at every camera location — this is a standard UK CCTV/ICO requirement and is a precondition of tenant onboarding. The tenant admin sign-off checkbox during tenant creation confirms this and records a `cctv_notice_confirmed_at` timestamp on the `TENANT#` record. SiteSpy is the processor; the tenant is the controller.

### 8.4 Subject access requests (SAR)
Per UK GDPR Article 15, individuals (typically workers on site) may request access to personal data held about them. Because the MVP does not perform any face recognition or tagging, SARs reduce to "provide snapshots from a given site during the period I was working there." The tenant admin handles these requests using the `bulk image export` endpoint (Phase 1) scoped to the relevant dates. The MVP-era answer is a manual S3 export run by the super admin on request.

### 8.5 Right to erasure (Article 17)
- **Dashboard users:** `DELETE /v1/users/{user_id}` anonymizes the Cognito record while preserving the audit trail under a "Deleted user" placeholder. Raw email is scrubbed.
- **Workers in images:** the MVP cannot selectively erase a worker from an image (no face recognition). On a valid erasure request, the tenant admin reviews the relevant images and either crops/blurs manually or deletes whole days. The super admin has a `DELETE /v1/admin/images` capability (gated, audit-logged, requires written reason) for this purpose. Must be added before first tenant onboard — tracked as a Phase 0 checklist item.

### 8.6 Breach notification
A breach of personal data must be reported to the ICO within 72 hours. Internal detection relies on CloudWatch alarms for:
- Unusual ingest credential failures (brute force).
- S3 policy changes outside SAM deploys.
- DynamoDB scans from unexpected principals.
- Cognito admin actions outside business hours.

Alarm routing is documented in `software_logic.md` §10 Observability.

### 8.7 Processor responsibilities
SiteSpy operates as a data processor for tenants. The tenant terms of service (drafted separately) MUST cover:
- Sub-processors (AWS, Slack, Met Office) with their locations.
- Right to audit.
- Sub-processor change notification period.
- Data return / deletion on contract end.

A DPA template is a Phase 0 commercial deliverable, not a software one — flagged here so it isn't forgotten.
