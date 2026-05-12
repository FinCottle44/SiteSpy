# SiteSpy — API Handover for Frontend Developers

This document contains everything you need to build a functional dashboard that consumes the SiteSpy API. The backend is deployed and operational — your job is to build the frontend that talks to it.

## Base URL

```
https://<api_id>.execute-api.eu-west-2.amazonaws.com/prod
```

All routes are prefixed with `/v1/`. The actual base URL will be provided as an environment variable:

```
VITE_API_ENDPOINT=https://xxxxxxxxxx.execute-api.eu-west-2.amazonaws.com/prod
```

## Authentication Header

Every dashboard API call requires:

```
Authorization: Bearer <cognito_id_token>
```

The ID token comes from the Cognito sign-in flow (see `auth_handover.md`). Tokens expire after 1 hour — use Amplify's built-in token refresh.

## Correlation ID

Include on every request for traceability:

```
X-Correlation-Id: <uuid-v4>
```

Generate a fresh UUID per request. The API returns it in responses for debugging.

---

## Endpoints

### GET /v1/tenants

Lists all tenants on the platform. **Super admin only.**

**Response (200):**
```json
{
  "tenants": [
    {
      "tenant_id": "acme",
      "tenant_name": "Acme Construction",
      "retention_years": 5
    }
  ]
}
```

Non-super-admins receive `403 ACCESS_DENIED`.

---

### GET /v1/sites

Returns all sites accessible to the caller.

**Query params:**
- `tenant_id` (required for super admins)

**Response (200):**
```json
{
  "sites": [
    {
      "site_id": "site_01",
      "site_name": "Red Construction - Main Site",
      "tenant_id": "acme",
      "latitude": 51.5074,
      "longitude": -0.1278,
      "timezone": "Europe/London"
    }
  ]
}
```

**Scope by role:**
- **Super admin:** all sites in the specified tenant
- **Tenant admin:** all sites in their tenant
- **User:** only sites in their `custom:site_access` list

---

### GET /v1/sites/{site_id}

Returns site metadata and its camera list. This is the first call after login — it tells you what cameras exist.

**Query params:**
- `tenant_id` (required for super admins only)

**Response (200):**
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

**Important:** The `timezone` field (IANA format, e.g. `Europe/London`) is the authoritative timezone for displaying all timestamps related to this site. Never use the browser's local timezone.

---

### GET /v1/snapshots/latest

Returns the most recent snapshot for a camera, or for all cameras in a site.

**Query params:**
- `site_id` (required)
- `camera_id` (optional — omit to get latest for ALL cameras)
- `tenant_id` (required for super admins only)

**Response — all cameras (200):**
```json
{
  "cameras": [
    {
      "camera_id": "cam_01",
      "camera_name": "North elevation",
      "timestamp": "2025-06-15T14:00:00Z",
      "presigned_url": "https://s3.eu-west-2.amazonaws.com/...",
      "expires_in": 300,
      "age_seconds": 1823
    },
    {
      "camera_id": "cam_02",
      "camera_name": "Crane cab",
      "timestamp": "2025-06-15T13:00:00Z",
      "presigned_url": "https://s3.eu-west-2.amazonaws.com/...",
      "expires_in": 300,
      "age_seconds": 5423
    }
  ]
}
```

**Response — single camera (200):**
```json
{
  "camera_id": "cam_01",
  "timestamp": "2025-06-15T14:00:00Z",
  "key": "acme_corp/site_001/cam_01/2025/06/15/2025-06-15T14:00:00Z.jpg",
  "presigned_url": "https://s3.eu-west-2.amazonaws.com/...",
  "expires_in": 300,
  "age_seconds": 1823
}
```

**Key field: `age_seconds`** — seconds since the last snapshot. Use this for the heartbeat indicator:
- Green: < 5400 (90 minutes)
- Yellow: 5400–10800 (90–180 minutes)
- Red: > 10800 (180 minutes)

**Key field: `presigned_url`** — a temporary S3 URL valid for 300 seconds (5 minutes). Use this directly as an `<img src>`. Do NOT cache these URLs — they expire. Fetch fresh ones when the user navigates back.

**Key field: `expires_in`** — always 300. You can use this to set a refresh timer.

If a camera has never received a snapshot, `timestamp`, `presigned_url`, and `age_seconds` will be `null`.

---

### GET /v1/snapshots

Returns a paginated list of snapshots for a specific camera within a date range.

**Query params:**
- `site_id` (required)
- `camera_id` (required)
- `from` (optional, ISO8601 date or datetime, defaults to 30 days ago)
- `to` (optional, ISO8601 date or datetime, defaults to now)
- `limit` (optional, 1–200, default 50)
- `cursor` (optional, opaque string from previous response)
- `tenant_id` (required for super admins only)

**Date format flexibility:**
- Date only: `2025-06-15` (expands to start-of-day or end-of-day automatically)
- Full datetime: `2025-06-15T14:00:00Z`

**Response (200):**
```json
{
  "images": [
    {
      "timestamp": "2025-06-15T14:00:00Z",
      "camera_id": "cam_01",
      "key": "acme_corp/site_001/cam_01/2025/06/15/2025-06-15T14:00:00Z.jpg",
      "presigned_url": "https://s3.eu-west-2.amazonaws.com/...",
      "expires_in": 300
    }
  ],
  "next_cursor": "eyJrZXkiOiAiLi4uIn0=",
  "total_available": 142
}
```

**Pagination:** Pass `next_cursor` back as `?cursor=<value>` to get the next page. When `next_cursor` is `null`, you've reached the end. Treat cursors as opaque — never parse or construct them.

**Ordering:** Results are returned newest-first (descending by timestamp).

---

### POST /v1/flags

Raises a flag on a specific camera. Any user with site access can do this.

**Body:**
```json
{
  "site_id": "site_001",
  "camera_id": "cam_01",
  "reason": "physical_damage",
  "note": "Mount has drooped ~30°, images now show the ground"
}
```

**Valid reasons:** `stale_image`, `physical_damage`, `obstruction`, `image_quality`, `other`

**Rules:**
- `note` is required when `reason` is `"other"`, optional otherwise
- `note` max length: 1000 characters
- Super admins must include `?tenant_id=<id>` as a query param

**Response — new flag (201):**
```json
{
  "flag_id": "01HXZ...",
  "status": "open",
  "raised_at": "2025-06-15T14:03:00Z"
}
```

**Response — duplicate suppression (200):**
If an open/acknowledged flag already exists for the same camera + reason:
```json
{
  "flag_id": "01HXZ...",
  "status": "open",
  "raised_at": "2025-06-14T09:00:00Z",
  "duplicate": true
}
```

Show different toast messages based on whether `duplicate` is present.

---

### GET /v1/flags

Lists flags, scoped by the caller's role.

**Query params:**
- `status` (optional, comma-separated, default: `open,acknowledged`)
- `tenant_id` (super admin only, optional)
- `site_id` (optional filter)
- `camera_id` (optional filter)
- `limit` (1–200, default 50)
- `cursor` (opaque pagination token)

**Response (200):**
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
        "presigned_url": "https://s3.eu-west-2.amazonaws.com/...",
        "expires_in": 300
      }
    }
  ],
  "next_cursor": null,
  "total_available": 1
}
```

**`latest_snapshot`** is a convenience field — the most recent image from that camera. Use it to show a thumbnail preview in the flag list without a second API call. May be `null` if the camera has never sent an image.

**`source`** is either `"user"` (human-raised) or `"auto"` (system-raised, e.g. stale image detection).

---

### PATCH /v1/flags/{flag_id}

Updates a flag's status. Min role: tenant admin.

**Query params:**
- `tenant_id` (required for super admins)

**Body:**
```json
{
  "status": "acknowledged",
  "admin_notes": "Site foreman notified, dispatching technician Friday."
}
```

**Valid transitions:**
- `open` → `acknowledged`, `resolved`, `dismissed`
- `acknowledged` → `resolved`, `dismissed`

Invalid transitions return `409 CONFLICT`.

**`admin_notes`** is optional, max 2000 characters.

**Response (200):**
```json
{
  "flag_id": "01HXZ...",
  "status": "acknowledged",
  "updated_at": "2025-06-15T15:00:00Z"
}
```

---

## Error Responses

All errors follow this shape:

```json
{
  "error": "ACCESS_DENIED",
  "message": "You do not have access to this site."
}
```

| HTTP Code | Error Key | Meaning | Frontend Action |
|---|---|---|---|
| 400 | `BAD_REQUEST` | Missing/malformed params | Show validation error |
| 401 | `UNAUTHORIZED` | Token expired or missing | Redirect to login |
| 403 | `ACCESS_DENIED` | Valid token but no permission | Show "Access Denied" page |
| 404 | `NOT_FOUND` | Resource doesn't exist | Show "Not Found" state |
| 409 | `CONFLICT` | Invalid flag transition | Show error toast |
| 429 | `TOO_MANY_REQUESTS` | Rate limited | Retry with backoff |
| 500 | `INTERNAL_ERROR` | Server failure | Show generic error with retry |

---

## Pre-signed URL Handling

Images are never served directly. The API returns temporary S3 URLs that expire after 5 minutes.

**Rules for the frontend:**
1. Use `presigned_url` directly as `<img src="...">` — no additional auth needed on the image request
2. URLs expire after 300 seconds — do NOT cache them in localStorage or state that persists across navigation
3. If an image fails to load (403 from S3), the URL has expired — re-fetch from the API
4. The S3 bucket has CORS configured to allow GET from any origin

**Recommended pattern:**
```typescript
// Fetch fresh URLs when entering a view
const { data } = await api.get('/v1/snapshots/latest', { params: { site_id } });

// Set a timer to refresh before expiry
setTimeout(() => refetch(), (data.expires_in - 30) * 1000);
```

---

## Pagination Pattern

All list endpoints use opaque cursor pagination:

```typescript
async function fetchAllFlags(siteId: string) {
  let cursor: string | null = null;
  const allFlags = [];

  do {
    const params: any = { site_id: siteId, limit: 50 };
    if (cursor) params.cursor = cursor;

    const { data } = await api.get('/v1/flags', { params });
    allFlags.push(...data.flags);
    cursor = data.next_cursor;
  } while (cursor);

  return allFlags;
}
```

---

## Timestamp Handling

**Critical rule:** All timestamps from the API are UTC ISO8601 (`2025-06-15T14:00:00Z`). Display them in the **site's timezone** (from `GET /v1/sites/{site_id}` → `timezone` field), never the browser's local timezone.

```typescript
function formatTimestamp(utcTimestamp: string, siteTimezone: string): string {
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: siteTimezone,
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(utcTimestamp));
}
```

---

## Rate Limits

| Consumer | Burst | Sustained |
|---|---|---|
| Regular user | 50 req/s | 20 req/s |
| Tenant admin | 100 req/s | 40 req/s |
| Super admin | 200 req/s | 80 req/s |

Exceeding limits returns `429` with a `Retry-After` header. Implement exponential backoff.

---

## CORS

The API allows:
- Methods: `GET, POST, PATCH, DELETE, OPTIONS`
- Headers: `Content-Type, Authorization, X-Correlation-Id`
- Origin: `*` (any origin allowed)

No special CORS handling needed on the frontend.

---

## Recommended API Client Setup

```typescript
import axios from 'axios';
import { fetchAuthSession } from 'aws-amplify/auth';
import { v4 as uuidv4 } from 'uuid';

const api = axios.create({
  baseURL: import.meta.env.VITE_API_ENDPOINT,
});

api.interceptors.request.use(async (config) => {
  const session = await fetchAuthSession();
  const token = session.tokens?.idToken?.toString();

  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  config.headers['X-Correlation-Id'] = uuidv4();

  return config;
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      // Token expired — redirect to login
      window.location.href = '/login';
    }
    return Promise.reject(error);
  }
);

export default api;
```

---

## Admin Management Endpoints

These endpoints handle provisioning of tenants, sites, cameras, and users. They require elevated roles (super admin or tenant admin).

---

### POST /v1/tenants

Creates a new tenant. **Super admin only.**

**Body:**
```json
{
  "tenant_id": "acme_corp",
  "tenant_name": "Acme Construction Ltd",
  "primary_contact_email": "ops@acme.example.com",
  "stale_threshold_hours": 24
}
```

**Field rules:**
- `tenant_id` — required, must match `^[a-z0-9_]{3,32}$`
- `tenant_name` — required
- `primary_contact_email` — required, must be a valid email
- `stale_threshold_hours` — optional, integer in [1, 720], defaults to 24

**Response (201):**
```json
{
  "tenant_id": "acme_corp",
  "tenant_name": "Acme Construction Ltd",
  "primary_contact_email": "ops@acme.example.com",
  "stale_threshold_hours": 24
}
```

**Errors:**
- 400 — invalid tenant_id format, missing required fields, stale_threshold_hours out of range
- 403 — caller is not super_admin
- 409 — tenant_id already exists

---

### POST /v1/sites

Creates a new site within a tenant. **Super admin only.**

**Query params:**
- `tenant_id` (required) — the tenant to create the site under

**Body:**
```json
{
  "site_id": "site_001",
  "site_name": "Acme Tower — Phase 2",
  "latitude": 51.5074,
  "longitude": -0.1278,
  "timezone": "Europe/London"
}
```

**Field rules:**
- `site_id` — required, must match `^[a-z0-9_]{1,64}$`
- `site_name` — required
- `latitude` — required, float in [-90, 90]
- `longitude` — required, float in [-180, 180]
- `timezone` — optional, valid IANA timezone, defaults to `Europe/London`

**Response (201):**
```json
{
  "site_id": "site_001",
  "site_name": "Acme Tower — Phase 2",
  "tenant_id": "acme_corp",
  "latitude": 51.5074,
  "longitude": -0.1278,
  "timezone": "Europe/London"
}
```

**Errors:**
- 400 — missing tenant_id query param, invalid site_id format, lat/lon out of range, invalid timezone
- 403 — caller is not super_admin
- 404 — tenant does not exist
- 409 — site_id already exists for this tenant

---

### POST /v1/sites/{site_id}/cameras

Registers a new camera on a site and mints an ingest token. **Super admin only.**

**Query params:**
- `tenant_id` (required)

**Body:**
```json
{
  "camera_id": "cam_01",
  "camera_name": "North elevation",
  "camera_model": "Axis P1455-LE"
}
```

**Field rules:**
- `camera_id` — required, must match `^[a-z0-9_]{1,64}$`
- `camera_name` — required
- `camera_model` — optional

**Response (201):**
```json
{
  "camera_id": "cam_01",
  "ingest_url": "https://<api_id>.execute-api.eu-west-2.amazonaws.com/prod/v1/ingest/tk_...",
  "ingest_token": "tk_8f2a4b9c1d7e3k5mQr9vL3kP7mN2xB8jY4hT5w"
}
```

**Important:** The `ingest_token` is the camera's authentication credential. It is embedded in the `ingest_url` path. Configure the camera to POST JPEG snapshots to `ingest_url`. The token can be rotated if compromised.

**Errors:**
- 400 — missing tenant_id, invalid camera_id format, missing required fields
- 403 — caller is not super_admin
- 404 — site does not exist
- 409 — camera_id already exists on this site

---

### GET /v1/sites/{site_id}/cameras

Lists all cameras on a site. **Tenant admin or super admin.**

**Query params:**
- `tenant_id` (required for super admins; tenant admins use their own tenant from JWT)

**Response (200):**
```json
{
  "cameras": [
    {
      "camera_id": "cam_01",
      "camera_name": "North elevation",
      "camera_model": "Axis P1455-LE"
    }
  ]
}
```

Returns an empty array if no cameras exist. Credentials are never included in this response.

**Errors:**
- 400 — super admin missing tenant_id query param
- 403 — caller lacks required role
- 404 — site does not exist

---

### POST /v1/sites/{site_id}/cameras/{camera_id}/rotate-credentials

Rotates the ingest token for a camera. **Tenant admin or super admin.**

**Query params:**
- `tenant_id` (required for super admins; tenant admins use their own tenant from JWT)

**No request body required.**

**Response (200):**
```json
{
  "camera_id": "cam_01",
  "ingest_url": "https://<api_id>.execute-api.eu-west-2.amazonaws.com/prod/v1/ingest/tk_...",
  "ingest_token": "tk_newRandom40charsHereAbcDefGhiJklMnoPqr"
}
```

**Important:** The old token is immediately invalidated. The camera must be reconfigured with the new `ingest_url` (which contains the new token).

**Errors:**
- 400 — super admin missing tenant_id query param
- 403 — caller lacks required role, or tenant admin accessing another tenant's camera
- 404 — camera does not exist
- 500 — token rotation failed (old token remains valid)

---

### POST /v1/users

Creates a new user in the system. **Tenant admin or super admin.**

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

**Field rules:**
- `email` — required, valid email format
- `full_name` — required
- `tenant_id` — required for super admins; tenant admins are scoped to their own tenant (field ignored)
- `role` — required, one of `user`, `tenant_admin`, `super_admin`
- `site_access` — required when role is `user`, list of valid site_ids for the tenant

**Role restrictions for tenant admins:**
- Cannot create `super_admin` users (403)
- Cannot create users in a different tenant (403)

**Response (201):**
```json
{
  "sub": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "email": "jane.doe@acme.example.com",
  "full_name": "Jane Doe",
  "tenant_id": "acme_corp",
  "role": "user",
  "site_access": ["site_001", "site_002"]
}
```

The user receives an invitation email from Cognito with a temporary password.

**Errors:**
- 400 — missing required fields, invalid email, invalid role, missing site_access for user role, invalid site_id in site_access
- 403 — caller lacks required role, tenant admin attempting cross-tenant or super_admin creation
- 409 — email already exists in the user pool

---

### GET /v1/users

Lists all users for a tenant. **Tenant admin or super admin.**

**Query params:**
- `tenant_id` (required for super admins; tenant admins use their own tenant from JWT)

**Response (200):**
```json
{
  "users": [
    {
      "sub": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "email": "jane.doe@acme.example.com",
      "full_name": "Jane Doe",
      "tenant_id": "acme_corp",
      "role": "user",
      "site_access": ["site_001", "site_002"]
    },
    {
      "sub": "f9e8d7c6-b5a4-3210-fedc-ba9876543210",
      "email": "admin@acme.example.com",
      "full_name": "Admin User",
      "tenant_id": "acme_corp",
      "role": "tenant_admin",
      "site_access": []
    }
  ]
}
```

Returns an empty array if no users exist for the tenant. The `site_access` field is an empty array for `tenant_admin` and `super_admin` roles (they have implicit access to all sites).

**Errors:**
- 400 — super admin missing tenant_id query param
- 403 — caller is a regular user (not tenant_admin or super_admin)
- 500 — DynamoDB query failure

---

### DELETE /v1/snapshots

Deletes one or more snapshot captures (removes both the DynamoDB record and the S3 image). **Tenant admin or super admin.**

**Query params:**
- `tenant_id` (required for super admins; tenant admins use their own tenant from JWT)

**Body:**
```json
{
  "site_id": "site_001",
  "camera_id": "cam_01",
  "timestamps": [
    "2025-06-15T14:00:00Z",
    "2025-06-15T13:00:00Z",
    "2025-06-15T12:00:00Z"
  ]
}
```

**Field rules:**
- `site_id` — required
- `camera_id` — required
- `timestamps` — required, array of ISO8601 UTC timestamp strings identifying the snapshots to delete
- Maximum 25 snapshots per request

**Response (200):**
```json
{
  "deleted": ["2025-06-15T14:00:00Z", "2025-06-15T13:00:00Z"],
  "deleted_count": 2,
  "not_found": ["2025-06-15T12:00:00Z"]
}
```

The `not_found` field is only present if some timestamps didn't match existing records. This is not an error — it allows idempotent retries.

**Errors:**
- 400 — missing required fields, timestamps not an array, exceeds batch limit (25)
- 403 — caller is a regular user
- 404 — site does not exist

---

### PATCH /v1/sites/{site_id}

Updates site configuration. Currently supports setting ingest hours. **Tenant admin or super admin.**

**Query params:**
- `tenant_id` (required for super admins; tenant admins use their own tenant from JWT)

**Body — set ingest hours:**
```json
{
  "ingest_hours": {
    "start": "07:00",
    "end": "18:00"
  }
}
```

**Body — clear ingest hours (allow all hours):**
```json
{
  "ingest_hours": null
}
```

**Field rules:**
- `ingest_hours` — object with `start` and `end` in HH:MM format (24-hour), or `null` to clear
- `start` and `end` must be different
- Supports overnight windows (e.g. `start: "22:00"`, `end: "06:00"`)
- Times are interpreted in the site's configured timezone

**Response (200):**
```json
{
  "site_id": "site_001",
  "tenant_id": "acme_corp",
  "ingest_hours": {
    "start": "07:00",
    "end": "18:00"
  }
}
```

**Behaviour:** When ingest hours are configured, the ingest endpoint (`POST /v1/ingest/{token}`) still accepts requests at any time (returns 200) but only saves the image to S3/DynamoDB if the current time (in the site's timezone) falls within the configured window. Outside the window, the ingest response includes `"status": "skipped"` instead of the usual `key`/`sha256` fields.

**Errors:**
- 400 — invalid time format, start equals end, missing fields
- 403 — caller is a regular user
- 404 — site does not exist

---

## What the API Does NOT Provide (Yet)

These endpoints are planned for future phases:

- `POST /v1/timelapses` — timelapse generation
- `POST /v1/exports` — bulk image export

The dashboard should be designed with these features in mind but they can be stubbed initially.
