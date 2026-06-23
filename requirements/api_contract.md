# API Contract

All endpoints are deployed via AWS API Gateway (REST) backed by Lambda (Python 3.12, SAM).

All URL paths are prefixed with `/v1/` from day one. Future breaking changes land on `/v2/` while `/v1/` continues to serve existing camera deployments.

---

## Authentication

| Consumer | Mechanism | Header |
| :--- | :--- | :--- |
| Dashboard (human users) | Cognito ID Token (JWT) | `Authorization: Bearer <id_token>` |
| Axis Camera (ingest) | HTTP Basic Auth (per-camera credential pair) | `Authorization: Basic <base64(username:password)>` |

**Why basic auth for ingest:** Axis VAPIX's HTTPS recipient only supports Basic Auth — it cannot send an arbitrary header like `x-api-key`. Per-camera credentials are preferred over a single shared key because they give us per-device revocation, cleaner audit trails, and per-camera rate limiting out of the box. Credentials are provisioned at camera registration time (see `POST /sites/{site_id}/cameras`) and stored in AWS Secrets Manager, never in DynamoDB.

Role resolution for dashboard endpoints follows `multi-tenant-auth.md` Section 4. Endpoints below annotate the minimum role required.

---

## Multi-Camera Sites

A **site** may contain one or more cameras. Each camera has a unique `camera_id` within its site, assigned during provisioning and recorded in the Site Mapping table. Every snapshot, flag, and timelapse is scoped to a specific camera, never just a site.

**Casing note:** the ingest endpoint accepts `cameraID` (camelCase) as a query parameter, matching the string currently configured in the Axis VAPIX recipient URL. All dashboard-facing endpoints use `camera_id` (snake_case) in query params, bodies, and responses — consistent with `site_id` and `tenant_id`. The ingest Lambda normalizes to `camera_id` internally.

---

## Endpoints

### POST /v1/ingest

Receives a raw JPEG snapshot from the Axis camera.

| Field | Value |
| :--- | :--- |
| Auth | `Authorization: Basic <base64(camera_username:camera_password)>` |
| Content-Type | `image/jpeg` |
| Query Params | `cameraID=<camera_id>` (required) |
| Required Headers | `X-Site-ID: <site_id>`, `X-Tenant-ID: <tenant_id>` |
| Body | Raw binary JPEG (1080p, ~200-500 KB) |
| Success Response | `201 Created` |

**Example URL:**
```
POST /v1/ingest?cameraID=cam_01
```

**Credential validation flow (ingest Lambda):**
1. Parse the `Authorization: Basic` header, decode username + password.
2. Look up the secret at `sitespy/cameras/<tenant_id>/<site_id>/<camera_id>` in AWS Secrets Manager.
3. Compare the submitted credentials against the stored pair using a constant-time comparison.
4. Cross-check that the `X-Tenant-ID`, `X-Site-ID`, and `cameraID` in the request match the secret's binding (defense in depth — even if a valid credential is stolen, it can only upload to its own camera path).
5. If any check fails → `401 UNAUTHORIZED`.

The credential pair is 32-character random usernames and 48-character random passwords, generated at registration time, shown **once** in the response to `POST /sites/{site_id}/cameras`, and never retrievable again. A lost credential is rotated by calling `POST /sites/{site_id}/cameras/{camera_id}/rotate-credentials`.

**Response body (201):**
```json
{
  "key": "<tenant_id>/<site_id>/<camera_id>/2025/06/15/2025-06-15T14:00:00Z.jpg",
  "timestamp": "2025-06-15T14:00:00Z",
  "camera_id": "cam_01",
  "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
}
```

**Validation:** The Lambda confirms the `TENANT#<tenant_id> / SITE#<site_id>#CAM#<camera_id>` record exists in the Site Mapping table before writing. Unknown camera IDs are rejected with `404 NOT_FOUND`.

**Idempotency:** If the same `<tenant_id> + <site_id> + <camera_id> + timestamp` combination is received twice, the Lambda MUST overwrite the existing S3 object (no duplicates).

**Image integrity (tamper-evidence foundation):** On successful write, the Lambda computes the SHA-256 of the uploaded JPEG bytes and persists an image record in DynamoDB:

| Field | Example |
| :--- | :--- |
| PK | `TENANT#acme_corp` |
| SK | `IMG#site_001#cam_01#2025-06-15T14:00:00Z` |

Attributes: `s3_key`, `sha256`, `size_bytes`, `ingested_at`, `content_type`. The hash is also written to S3 object metadata (`x-amz-meta-sha256`) so tooling outside the API can verify it without a DynamoDB read. This data is inert in Phase 0 — no feature reads it yet — but it is the foundation for the tamper-evident Dispute Mode packets in the roadmap backlog. Adding it later would require a full archive rehash, which is expensive, hence its inclusion now.

**Stale-image auto-flag:** On successful ingest, the Lambda MUST clear any open `stale_image` auto-flag for this specific camera. Staleness is tracked per camera, not per site. See `software_logic.md` Section 6.

---

### GET /v1/snapshots

Returns a paginated list of available snapshots for a given camera.

| Field | Value |
| :--- | :--- |
| Auth | `Authorization: Bearer <id_token>` (min role: `user`) |
| Query Params | `site_id` (required), `camera_id` (required), `from` (ISO8601, optional), `to` (ISO8601, optional), `limit` (int, default 50, max 200), `cursor` (opaque string, optional) |

`camera_id` is required. To list snapshots across all cameras in a site, the client makes one call per camera (the site's camera list is available from `GET /v1/sites/{site_id}`).

**Response body (200):**
```json
{
  "images": [
    {
      "timestamp": "2025-06-15T14:00:00Z",
      "camera_id": "cam_01",
      "key": "acme_corp/site_001/cam_01/2025/06/15/2025-06-15T14:00:00Z.jpg",
      "presigned_url": "https://s3.amazonaws.com/...",
      "expires_in": 300
    }
  ],
  "next_cursor": "eyJrZXkiOiAiLi4uIn0=",
  "total_available": 142
}
```

**Implementation:** The Lambda calls `s3:ListObjectsV2` with prefix `<tenant_id>/<site_id>/<camera_id>/<year>/<month>/<day>/`, filtered by the `from`/`to` range. Pre-signed URLs are generated with a 5-minute TTL.

---

### GET /v1/snapshots/latest

Returns the single most recent snapshot for a given camera, or for every camera in a site when `camera_id` is omitted.

| Field | Value |
| :--- | :--- |
| Auth | `Authorization: Bearer <id_token>` (min role: `user`) |
| Query Params | `site_id` (required), `camera_id` (optional) |

**Behavior:**
- With `camera_id`: returns the latest snapshot for that single camera (same shape as before, plus `camera_id`).
- Without `camera_id`: returns the latest snapshot for every camera in the site. This powers the multi-camera hero view.

**Response body (200) — single camera:**
```json
{
  "camera_id": "cam_01",
  "timestamp": "2025-06-15T14:00:00Z",
  "key": "acme_corp/site_001/cam_01/2025/06/15/2025-06-15T14:00:00Z.jpg",
  "presigned_url": "https://s3.amazonaws.com/...",
  "expires_in": 300,
  "age_seconds": 1823
}
```

**Response body (200) — all cameras in site:**
```json
{
  "cameras": [
    {
      "camera_id": "cam_01",
      "camera_name": "North elevation",
      "timestamp": "2025-06-15T14:00:00Z",
      "presigned_url": "https://s3.amazonaws.com/...",
      "expires_in": 300,
      "age_seconds": 1823
    },
    {
      "camera_id": "cam_02",
      "camera_name": "Crane cab",
      "timestamp": "2025-06-15T13:00:00Z",
      "presigned_url": "https://s3.amazonaws.com/...",
      "expires_in": 300,
      "age_seconds": 5423
    }
  ]
}
```

`age_seconds` drives the heartbeat indicator per camera.

---

### GET /v1/sites/{site_id}

Returns the metadata for a site, including its camera list. Used by the dashboard to render the camera selector and the multi-camera hero view.

| Field | Value |
| :--- | :--- |
| Auth | `Authorization: Bearer <id_token>` (min role: `user`) |
| Path Params | `site_id` (required) |

**Response body (200):**
```json
{
  "site_id": "site_001",
  "site_name": "Acme Tower — Phase 2",
  "tenant_id": "acme_corp",
  "latitude": 51.5074,
  "longitude": -0.1278,
  "timezone": "Europe/London",
  "cameras": [
    {
      "camera_id": "cam_01",
      "camera_name": "North elevation",
      "camera_model": "Axis P1455-LE"
    },
    {
      "camera_id": "cam_02",
      "camera_name": "Crane cab",
      "camera_model": "Axis P1455-LE"
    }
  ]
}
```

---

### POST /v1/sites

Registers a new site under the caller's tenant. Min role: **tenant admin** or **super admin**.

**Body:**
```json
{
  "site_id": "site_001",
  "site_name": "Acme Tower — Phase 2",
  "latitude": 51.5074,
  "longitude": -0.1278,
  "timezone": "Europe/London",
  "address": "1 Example Street, London"
}
```

**Validation:**
- `site_id` must be unique within the tenant (enforced via a `ConditionExpression` on DynamoDB write).
- `latitude` must be in `[-90, 90]`, `longitude` in `[-180, 180]`. Both are required.
- `timezone` must be a valid IANA zone. Defaults to `Europe/London` when omitted.
- Super admins MUST supply `tenant_id` as a query param; tenant admins use their own tenant implicitly.

Returns `201 Created` with the full site record. Camera registration is handled by `POST /v1/sites/{site_id}/cameras` below.

---

## Tenant Management (Super Admin)

### POST /v1/tenants

Creates a new tenant. Min role: **super admin**.

**Body:**
```json
{
  "tenant_id": "acme_corp",
  "tenant_name": "Acme Construction Ltd",
  "primary_contact_email": "ops@acme.example.com",
  "stale_threshold_hours": 24
}
```

- `tenant_id` is a lowercase slug (regex: `^[a-z0-9_]{3,32}$`), enforced unique via `ConditionExpression`. Used as the S3 prefix and the Cognito attribute value — cannot be changed after creation.
- `tenant_name` is the display name shown in the UI.
- `stale_threshold_hours` is optional (default 24). Controls when the stale-image auto-flag fires.

Returns `201 Created`. The super admin must still create the first `TenantAdmin` user via `POST /v1/users`.

### GET /v1/tenants

Lists all tenants. Super admin only. Used to populate the tenant picker.

**Sandbox visibility:** The hidden `sandbox_construction` tenant is filtered from responses for non-super_admin roles. Since this endpoint is currently super_admin-only, this is a future-proofing measure.

### PATCH /v1/tenants/{tenant_id}

Updates a tenant's display name, contact email, or staleness threshold. Cannot change `tenant_id`. Super admin only.

### DELETE /v1/tenants/{tenant_id}

Soft-deletes a tenant. Super admin only. Requires confirmation header `X-Confirm-Delete: <tenant_id>` to reduce accidental loss. Implementation:

1. Marks the tenant record `status = "deleted"` and stamps `deleted_at`.
2. Disables all Cognito users belonging to the tenant.
3. Does **not** delete S3 images immediately — a scheduled purge Lambda removes them after the tenant's image retention window (see `multi-tenant-auth.md` §8). This preserves dispute evidence during the wind-down.
4. Returns `202 Accepted` with an audit ID.

---

## User Management

User records live in Cognito. The management API is a thin layer on top of Cognito Admin SDK calls, adding tenant-scoping and audit logging.

### POST /v1/users

Creates a user. Min role: **tenant admin** (within own tenant) or **super admin** (any tenant).

**Body:**
```json
{
  "email": "jane.doe@acme.example.com",
  "full_name": "Jane Doe",
  "tenant_id": "acme_corp",
  "role": "user",
  "site_access": ["site_001", "site_002"]
}
```

- `role` is one of `user`, `tenant_admin`, `super_admin`. Only super admins may create `super_admin` or cross-tenant users.
- `site_access` is required only when `role == "user"`.
- Cognito sends an invitation email with a temporary password. The user must change the password on first login.

Returns `201 Created` with the Cognito `sub`.

### GET /v1/users

Lists users. Tenant admin sees users in their tenant; super admin sees all users with an optional `tenant_id` filter.

Supports `cursor` pagination and `q` (email substring filter).

### GET /v1/users/{user_id}

Returns a single user record including their current `tenant_id`, role, and `site_access`.

### PATCH /v1/users/{user_id}

Updates name, role, or site_access. Cannot change `tenant_id` — to move a user, delete and recreate. Tenant admins cannot promote a user to `super_admin` or out of their tenant.

### PATCH /v1/users/{user_id}/site-access

Convenience endpoint for the common "assign a user to sites" operation. Body: `{ "site_access": ["site_001", "site_003"] }`. Replaces the full list (not a delta).

### DELETE /v1/users/{user_id}

Disables the Cognito user and anonymizes their profile (`email → redacted+<uuid>@deleted.invalid`, `full_name → "Deleted user"`). Their audit trail (flags raised, exclusions, annotations) remains with the anonymized name. This is the path used for GDPR right-to-erasure requests — see `multi-tenant-auth.md` §8.

Tenant admins may delete users in their own tenant. Super admins may delete any user.

### POST /v1/users/{user_id}/resend-invitation

Re-sends the Cognito invitation email if the user never completed first login.

---

## Camera Management

### POST /v1/sites/{site_id}/cameras

Registers a new camera on a site and mints its ingest credentials. Min role: **tenant admin** or **super admin**.

**Body:**
```json
{
  "camera_id": "cam_01",
  "camera_name": "North elevation",
  "camera_model": "Axis P1455-LE"
}
```

**Response body (201) — credentials shown once:**
```json
{
  "camera_id": "cam_01",
  "ingest_credentials": {
    "username": "sitespy_cam_8f2a4b9c1d7e",
    "password": "Qr9vL3kP7mN2xB8jY4hT5wF6sD1gA0cE"
  },
  "ingest_url": "https://<api_id>.execute-api.eu-west-2.amazonaws.com/prod/v1/ingest?cameraID=cam_01",
  "ingest_headers": {
    "X-Tenant-ID": "acme_corp",
    "X-Site-ID": "site_001"
  }
}
```

The password is **shown once, never stored in plaintext outside Secrets Manager, and never returned by any subsequent GET**. If lost, use `rotate-credentials`. The response shape is deliberately copy-pasteable into the Axis VAPIX recipient configuration.

### GET /v1/sites/{site_id}/cameras/{camera_id}

Returns camera metadata (no credentials). Any user with site access.

### PATCH /v1/sites/{site_id}/cameras/{camera_id}

Updates `camera_name` or `camera_model`. `camera_id` is immutable. Tenant admin or above.

### POST /v1/sites/{site_id}/cameras/{camera_id}/rotate-credentials

Generates a new credential pair for the camera, stores it in Secrets Manager (replacing the previous version), and returns the new pair **once**. Tenant admin or above.

The old credential is immediately invalidated. Any in-flight ingest from the camera will fail with `401` until the Axis device is reconfigured with the new pair.

### POST /v1/cameras/transfer

Atomically moves a camera from one tenant/site to another, preserving the ingest token so the physical camera continues working without reconfiguration. **Super admin only.**

This is the mechanism for transferring cameras from the hidden sandbox tenant (`sandbox_construction`) to a customer tenant after provisioning and testing, but it works between any two tenants.

**Body:**
```json
{
  "source_tenant_id": "sandbox_construction",
  "source_site_id": "default_sandbox_site",
  "camera_id": "cam_01",
  "target_tenant_id": "acme_corp",
  "target_site_id": "site_001"
}
```

All five fields are required non-empty strings.

**Response body (200):**
```json
{
  "tenant_id": "acme_corp",
  "site_id": "site_001",
  "camera_id": "cam_01"
}
```

**Atomicity:** The transfer uses a DynamoDB `transact_write_items` — the target record is created and the source record is deleted in a single transaction. At no point does the GSI1 token index resolve to zero or two records, so ingestion continues uninterrupted during transfer.

**What transfers:** `camera_id`, `camera_name`, `camera_model`, `ingest_token` (and its GSI1 mapping). A `transferred_at` timestamp is added to the target record.

**What does NOT transfer:** Snapshot records (`IMG#`) remain under the source tenant. They are test data and are not moved.

**Error responses:**

| Condition | Code | Error Key | Message |
|-----------|------|-----------|---------|
| Caller is not super_admin | 403 | `ACCESS_DENIED` | "You do not have access to this resource." |
| Missing/empty required field | 400 | `BAD_REQUEST` | "Missing required field: {field_name}." |
| Source camera not found | 404 | `NOT_FOUND` | "Source camera not found." |
| Target tenant not found | 404 | `NOT_FOUND` | "Target tenant not found." |
| Target site not found or doesn't belong to target tenant | 404 | `NOT_FOUND` | "Target site not found." |
| Camera already exists at target site | 409 | `CONFLICT` | "A camera with this camera_id already exists at the target site." |
| Transaction failure | 500 | `INTERNAL_ERROR` | "An internal error occurred." |

---

### DELETE /v1/sites/{site_id}/cameras/{camera_id}

Deletes the camera record, invalidates its credentials, and leaves historical images in S3 untouched (they stay accessible under the tenant's retention policy). Tenant admin or above.

---

## Flagged Cameras

A **flag** is a signal that something is wrong with a specific camera — stale image, physical damage, drooped mount, obstruction, image quality, etc. Flags are either raised manually by any authenticated user who can view the site, or raised automatically by the staleness detector. Every flag notifies super admins via a Slack webhook. See `software_logic.md` Section 6 for the notification pipeline.

### DynamoDB model (same table as Site Mapping)

| Field | Example |
| :--- | :--- |
| PK | `TENANT#acme_corp` |
| SK | `FLAG#site_001#cam_01#2025-06-15T14:03:00Z` |
| GSI1PK | `FLAGSTATUS#open` |
| GSI1SK | `2025-06-15T14:03:00Z` |

`GSI1` lets super admins list all open flags across every tenant in one query.

**Attributes:** `flag_id` (ULID), `tenant_id`, `site_id`, `camera_id`, `reason`, `note`, `status`, `source` (`user` \| `auto`), `raised_by` (Cognito `sub`, or `system`), `raised_at`, `acknowledged_by`, `acknowledged_at`, `resolved_by`, `resolved_at`, `admin_notes`.

**Reason values** (enum): `stale_image`, `physical_damage`, `obstruction`, `image_quality`, `other`. When `reason == "other"`, the `note` field is required.

**Status lifecycle:** `open → acknowledged → resolved`. Any state may transition to `dismissed` (false positive). Transitions are append-only — the record keeps the full timeline of who did what.

---

### POST /v1/flags

Raises a new flag on a specific camera. Open to any authenticated user with access to the site.

| Field | Value |
| :--- | :--- |
| Auth | min role: `user` |
| Body | `{ "site_id": "site_001", "camera_id": "cam_01", "reason": "physical_damage", "note": "Mount has drooped ~30°, images now show the ground" }` |

**Validation:**
- `site_id` must be in the caller's `site_access` (or any site in their tenant for tenant admins, or any site for super admins).
- `camera_id` must exist under `TENANT#<tenant_id> / SITE#<site_id>` in the Site Mapping table.
- `reason` must be one of the enum values. `note` is required for `reason == "other"`, optional otherwise (max 1000 chars).
- If an **open** or **acknowledged** flag already exists for the same `camera_id` with the same `reason`, return the existing flag with `200 OK` instead of creating a duplicate. Duplicate suppression is per-camera, so two cameras at the same site can carry the same reason simultaneously.

**Response body (201):**
```json
{
  "flag_id": "01HXZ...",
  "status": "open",
  "raised_at": "2025-06-15T14:03:00Z"
}
```

**Side effect:** Posts a message to the configured Slack webhook (see `software_logic.md` Section 6).

---

### GET /v1/flags

Lists flags, scoped by the caller's role.

| Field | Value |
| :--- | :--- |
| Auth | min role: `user` |
| Query Params | `status` (optional, default `open,acknowledged`), `tenant_id` (super admin only), `site_id` (optional), `camera_id` (optional), `limit` (default 50, max 200), `cursor` (optional) |

**Scope by role:**
- **Super admin:** all tenants by default. `tenant_id` filters to one tenant. Uses the `GSI1` index when no tenant filter is applied.
- **Tenant admin:** flags within their tenant only.
- **User:** flags on sites in their `site_access` only.

**Response body (200):**
```json
{
  "flags": [
    {
      "flag_id": "01HXZ...",
      "tenant_id": "acme_corp",
      "site_id": "site_001",
      "camera_id": "cam_01",
      "reason": "stale_image",
      "note": null,
      "status": "open",
      "source": "auto",
      "raised_by": "system",
      "raised_at": "2025-06-15T14:03:00Z",
      "latest_snapshot": {
        "timestamp": "2025-06-12T09:00:00Z",
        "presigned_url": "https://s3.amazonaws.com/...",
        "expires_in": 300
      }
    }
  ],
  "next_cursor": null,
  "total_available": 1
}
```

The `latest_snapshot` convenience field is the most recent snapshot for that specific camera, letting the admin console preview the issue without a second round-trip.

---

### PATCH /v1/flags/{flag_id}

Updates a flag's status or admin notes. Min role: **tenant admin** (own tenant only) or **super admin**.

**Body:**
```json
{
  "status": "acknowledged",
  "admin_notes": "Site foreman notified, dispatching technician Friday."
}
```

Allowed transitions: `open → acknowledged`, `open → resolved`, `open → dismissed`, `acknowledged → resolved`, `acknowledged → dismissed`. The Lambda records the acting user and timestamp. Invalid transitions return `409 CONFLICT`.

---

## Timelapse Generation (Future — not MVP)

The timelapse generator compiles a date range into an MP4 via a server-side FFmpeg Lambda. Each render is scoped to a single camera — you cannot mix multiple cameras into one timelapse. This section defines the contract so the dashboard and exclusion UX can be designed against it now. See `dashboard.md` Section 6 for the user-facing flow.

### POST /v1/timelapses

Requests a new timelapse render. Min role: `user`.

**Body:**
```json
{
  "site_id": "site_001",
  "camera_id": "cam_01",
  "from": "2025-06-01T00:00:00Z",
  "to": "2025-06-15T23:59:59Z",
  "fps": 12,
  "exclusions": [
    { "from": "2025-06-08T14:00:00Z", "to": "2025-06-10T09:00:00Z", "reason": "Camera fell off mount" },
    { "timestamps": ["2025-06-12T11:00:00Z", "2025-06-12T12:00:00Z"], "reason": "Obstructed by crane" }
  ]
}
```

`camera_id` is required. To produce a timelapse per camera on a multi-camera site, the dashboard submits one request per camera.

Exclusions accept either a `from`/`to` window or an explicit `timestamps` array — the dashboard offers both UX options. Any snapshot whose timestamp falls inside a window, or exactly matches a listed timestamp, is skipped by FFmpeg. Exclusions are **one-shot** — they apply to this render only and are not stored on the site or camera. A `reason` string (max 500 chars) is required per exclusion for audit purposes.

**Response body (202):**
```json
{
  "timelapse_id": "01HY0...",
  "status": "queued"
}
```

The render runs async. A separate `GET /timelapses/{id}` endpoint returns status and, on completion, a pre-signed URL to the MP4.

### Audit

The submitted exclusion list (including `reason`, the acting user's `sub`, and the submission timestamp) is persisted alongside the timelapse record in DynamoDB. Tenant admins and super admins can retrieve the audit via `GET /timelapses/{id}` — useful for dispute resolution in construction contexts. Excluded images are **never** deleted or tagged in S3; exclusions only affect the render pipeline.

### Visibility

By default the resulting MP4 is visible to all users in the site (everyone with that `site_id` in `site_access`), plus tenant admins. The requester's identity and the exclusion audit are visible to tenant admins and super admins.

---

## Error Responses

All errors follow this shape:

```json
{
  "error": "ACCESS_DENIED",
  "message": "You do not have access to this site."
}
```

| Code | Error Key | Meaning |
| :--- | :--- | :--- |
| 400 | `BAD_REQUEST` | Missing or malformed parameters |
| 401 | `UNAUTHORIZED` | Missing or expired token |
| 403 | `ACCESS_DENIED` | Token valid but user lacks site access |
| 404 | `NOT_FOUND` | Site, camera, image, or flag does not exist |
| 409 | `CONFLICT` | Duplicate ingest (informational, image was overwritten) OR invalid flag state transition |
| 500 | `INTERNAL_ERROR` | Unexpected server failure |

---

## S3 Key Structure (Canonical)

```
<bucket>/<tenant_id>/<site_id>/<camera_id>/<YYYY>/<MM>/<DD>/<YYYY-MM-DDTHH:mm:ssZ>.jpg
```

Example:
```
project-snapshots/acme_corp/site_001/cam_01/2025/06/15/2025-06-15T14:00:00Z.jpg
```

All timestamps are UTC ISO8601. No Unix timestamps anywhere in the system.

**Migration note:** The `camera_id` segment is new. If any pre-multi-camera snapshots exist at the old path (`<tenant_id>/<site_id>/<YYYY>/...`), a one-time migration script must move them under a default `camera_id` (e.g., `cam_01`) before any dashboard reads hit the new prefix structure. This is an ops task, not a runtime behavior — the Lambda does not read legacy paths.

---

## Pagination

All list endpoints use **opaque cursor pagination**.

- Responses that support pagination include `next_cursor` (string or null) and `total_available` (integer, best-effort).
- `next_cursor` is a base64-encoded JSON object produced by the server. Clients MUST treat it as opaque — do not parse, modify, or construct cursors on the client.
- To fetch the next page, the client passes the cursor back as `?cursor=<value>`. An absent or null `next_cursor` indicates the end of the result set.
- Cursors are not long-lived. Records inserted after a cursor is issued may be skipped or duplicated on resumption. For "give me everything from yesterday" the client should always pass an explicit `from`/`to` range.

---

## Rate Limits

API Gateway usage plans enforce per-consumer limits. Defaults at launch:

| Consumer | Burst | Sustained |
| :--- | :--- | :--- |
| Dashboard user | 50 req/s | 20 req/s |
| Tenant admin | 100 req/s | 40 req/s |
| Super admin | 200 req/s | 80 req/s |
| Camera (per credential pair) | 2 req/s | 1 req/min |

Camera limits are tight on purpose — snapshots are hourly, any burst above 2/s means something is wrong. Exceeding a limit returns `429 TOO_MANY_REQUESTS` with a `Retry-After` header.

Limits are tunable per-tenant via a `rate_limit_overrides` attribute on the `TENANT#` record for customers that need more.
