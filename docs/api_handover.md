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
- Methods: `GET, POST, PATCH, OPTIONS`
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

## What the API Does NOT Provide (Yet)

These endpoints are documented in the API contract but not yet implemented (future phases):

- `POST /v1/tenants` — tenant CRUD (super admin)
- `POST /v1/users` — user management
- `POST /v1/sites` — site creation
- `POST /v1/sites/{site_id}/cameras` — camera registration
- `POST /v1/timelapses` — timelapse generation
- `POST /v1/exports` — bulk image export

For MVP, tenant/site/camera provisioning is done via CLI scripts. The dashboard should be designed with these admin UIs in mind but they can be stubbed initially.
