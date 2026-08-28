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
      "timezone": "Europe/London",
      "working_hours": {
        "days": ["mon", "tue", "wed", "thu", "fri"],
        "start": "07:00",
        "end": "18:00"
      }
    }
  ]
}
```

**Key field: `working_hours`** — the site's working-hours window (days of week + HH:MM time range), or `null` when unset. Snapshots captured inside this window get long-term retention; those captured outside get a fixed 7-day expiry. See `PATCH /v1/sites/{site_id}` for the full model. `null` means every snapshot is treated as in-hours.

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
  "working_hours": {
    "days": ["mon", "tue", "wed", "thu", "fri"],
    "start": "07:00",
    "end": "18:00"
  },
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

**Key field: `working_hours`** — `{days, start, end}` or `null`. Working hours are evaluated in the site's `timezone` to classify each captured snapshot's retention (see `PATCH /v1/sites/{site_id}`). `null` means every snapshot is treated as in-hours. The legacy `ingest_hours` field is no longer returned.

---

### GET /v1/snapshots/latest

Returns the most recent snapshot for a camera, or for all cameras in a site.

**In-hours only:** This endpoint returns only in-hours snapshots (those captured within the site's `working_hours`). Out-of-hours snapshots are never selected here — review them via `GET /v1/snapshots/out-of-hours`. For a single camera with no in-hours snapshot, the endpoint returns `404 NOT_FOUND`.

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

**In-hours only:** This list returns only in-hours snapshots (captured within the site's `working_hours`). Out-of-hours snapshots are excluded — list them via `GET /v1/snapshots/out-of-hours`.

**Query params:**
- `site_id` (required)
- `camera_id` (required)
- `from` (optional, ISO8601 date or datetime, defaults to 30 days ago)
- `to` (optional, ISO8601 date or datetime, defaults to now)
- `limit` (optional, 1–200, default 50) — ignored when `sample` is set
- `sample` (optional, 1–500) — **preview mode**, see below
- `order` (optional, `asc` | `desc`, default `desc`) — `asc` returns oldest-first, `desc` newest-first. With `limit=1`, `order=asc` gives the earliest snapshot in the range and `order=desc` the latest.
- `cursor` (optional, opaque string from previous response) — cannot be combined with `sample`
- `tenant_id` (required for super admins only)

**Date format flexibility:**
- Date only: `2025-06-15` (expands to start-of-day or end-of-day automatically)
- Full datetime: `2025-06-15T14:00:00Z`

**Ordering & pagination:** keep `order` constant across a cursor sequence — a `next_cursor` is tied to the direction it was issued in and can't be resumed under the opposite order.

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

**Ordering:** Controlled by `order` (default `desc` = newest-first; `asc` = oldest-first).

**`total_available` caveat:** in the default (non-sampled) mode this is *not* a true count of snapshots in the range — it is the number of items on the current page, plus 1 when another page exists. Treat it as a "there is more" signal, not a total. In sampled mode it is the exact number of images returned.

#### Preview mode: `?sample=N`

Returns up to `N` snapshots spread **evenly across the whole `from`–`to` window** instead of one contiguous page. Built for scrubber/preview UIs that need to represent an entire period without rendering a timelapse.

```
GET /v1/snapshots?site_id=site_001&camera_id=cam_01
                 &from=2025-06-01T00:00:00Z&to=2025-06-08T00:00:00Z
                 &sample=120&order=asc
```

How it works: the window is divided into `N` equal-width **time** buckets and the earliest snapshot in each bucket is returned. Spacing is even in *time*, not by index, so a scrubber's slider position maps linearly onto the window even when capture density varies.

What this means in practice:
- **Cost is bounded by `N`, not by how many snapshots exist.** A 7-day window with 700 snapshots and a 30-day window with 40,000 both cost `N` reads. Pick `N` to match your timeline's pixel width.
- **You may get fewer than `N` images.** Buckets with no snapshots contribute nothing, so genuine gaps in coverage (camera offline, out-of-hours periods) stay visible rather than being silently smoothed over. Do not assume `images.length == sample`.
- **Not paginated.** `next_cursor` is always `null`; the response already covers the whole window. Combining `sample` with `cursor` returns `400`.
- `limit` is ignored when `sample` is set.
- `order` still applies to the returned array (`asc` = oldest-first, recommended for a scrubber).

**Additional response fields in sampled mode:**
```json
{
  "images": [ /* ...same shape as above... */ ],
  "next_cursor": null,
  "total_available": 37,
  "sampled": true,
  "sample_requested": 50,
  "window": { "from": "2025-06-01T00:00:00Z", "to": "2025-06-08T00:00:00Z" }
}
```

- `sampled` — always `true` here; absent in the default mode, so you can tell the two responses apart.
- `sample_requested` — the `N` you asked for (compare against `images.length` to detect coverage gaps).
- `window` — the resolved `from`/`to` after date-only expansion and defaulting. Use these bounds, not your own request values, to position frames on a timeline.

**Presigned URL expiry:** `expires_in` is `300` seconds for every image, sampled or not. A scrubber left open longer than 5 minutes will have dead image URLs. There is no batch URL-refresh endpoint — re-issue the same request to mint fresh URLs.

**Relationship to rendered timelapses:** preview frames are selected by time bucket, whereas the renderer selects evenly by index across the available frames. The two sets will therefore differ where capture density is uneven. Preview mode is for *scrubbing a period*, not for previewing the exact output of `POST /v1/timelapse-jobs`.

---

## Out-of-Hours Snapshots

Snapshots captured **outside** a site's `working_hours` are saved with a fixed **7-day (604800 s)** expiry — after that they are auto-deleted unless **promoted**. They are excluded from the default `GET /v1/snapshots` and `GET /v1/snapshots/latest` views. The flow is: **review** out-of-hours snapshots for a camera (`GET`), **promote** the ones worth keeping so they escape the 7-day expiry (`POST`), then **download** a preserved snapshot via a presigned URL (`GET`).

A snapshot is identified for promote/download by its capture timestamp (`snapshot_id`), combined with `site_id` and `camera_id`.

---

### GET /v1/snapshots/out-of-hours

Lists out-of-hours snapshots for a camera within a date range, newest-first. Same tenant/site access rules as the other snapshot endpoints.

**Query params:**
- `site_id` (required)
- `camera_id` (required)
- `from` (optional, `YYYY-MM-DDTHH:MM:SSZ` UTC datetime)
- `to` (optional, `YYYY-MM-DDTHH:MM:SSZ` UTC datetime)
- `limit` (optional, 1–200, default 50)
- `cursor` (optional, opaque string from a previous response)
- `tenant_id` (required for super admins only)

**Date range default:** When `from`/`to` are omitted, the range covers the **30 days** immediately preceding the request. Both bounds are inclusive.

**Response (200):**
```json
{
  "snapshots": [
    {
      "snapshot_id": "2025-06-15T02:00:00Z",
      "timestamp": "2025-06-15T02:00:00Z",
      "camera_id": "cam_01",
      "key": "security/acme_corp/site_001/cam_01/2025/06/15/2025-06-15T02:00:00Z.jpg",
      "presigned_url": "https://s3.eu-west-2.amazonaws.com/...",
      "expires_in": 300,
      "promoted": false
    }
  ],
  "next_cursor": "eyJrZXkiOiAiLi4uIn0="
}
```

**Field notes:**
- `snapshot_id` — the capture timestamp; pass it to promote/download.
- `presigned_url` / `expires_in` — a temporary review URL valid for **300 seconds** (5 minutes). Do not cache beyond expiry.
- `promoted` — `true` once the snapshot has been preserved past the 7-day expiry, `false` while it is still subject to it.
- `next_cursor` — pass back as `?cursor=<value>` for the next page; `null` means the last page. Treat cursors as opaque.

When no snapshots match, `snapshots` is an empty array with `next_cursor: null`.

**Errors:**
- 400 — missing `site_id`/`camera_id`, invalid `from`/`to` datetime, `limit` outside 1–200, malformed `cursor`, or super admin missing `tenant_id`
- 403 — caller lacks access to the site

---

### POST /v1/snapshots/out-of-hours/promote

Promotes ("saves") an out-of-hours snapshot so it is preserved beyond the 7-day expiry and remains downloadable. Idempotent — promoting an already-promoted snapshot is a no-op success. Same tenant/site access rules as the other snapshot endpoints.

**Query params:**
- `tenant_id` (required for super admins only)

**Body:**
```json
{
  "site_id": "site_001",
  "camera_id": "cam_01",
  "snapshot_id": "2025-06-15T02:00:00Z"
}
```

All three fields are required.

**Response (200):**
```json
{
  "snapshot_id": "2025-06-15T02:00:00Z",
  "site_id": "site_001",
  "camera_id": "cam_01",
  "promoted": true,
  "key": "preserved/acme_corp/site_001/cam_01/2025/06/15/2025-06-15T02:00:00Z.jpg"
}
```

**What happens on promotion:** The stored object is relocated from the `security/` prefix (7-day expiry) to the `preserved/` prefix (no expiry), and the snapshot's expiry timer is removed. Once promoted, the snapshot survives past the 7-day window and can be downloaded. For an already-promoted snapshot the response is returned unchanged and the `key` field may be omitted (no relocation occurs).

**Errors:**
- 400 — missing `site_id`, `camera_id`, or `snapshot_id`
- 403 — caller lacks access to the site
- 404 — snapshot does not exist, is not an out-of-hours snapshot, or has already expired
- 500 — promotion did not complete; the snapshot remains under its original 7-day expiry

---

### GET /v1/snapshots/out-of-hours/download

Returns a presigned download URL for an out-of-hours snapshot (typically one that has been promoted). Same tenant/site access rules as the other snapshot endpoints.

**Query params:**
- `site_id` (required)
- `camera_id` (required)
- `snapshot_id` (required — the capture timestamp)
- `tenant_id` (required for super admins only)

**Response (200):**
```json
{
  "snapshot_id": "2025-06-15T02:00:00Z",
  "camera_id": "cam_01",
  "timestamp": "2025-06-15T02:00:00Z",
  "key": "preserved/acme_corp/site_001/cam_01/2025/06/15/2025-06-15T02:00:00Z.jpg",
  "presigned_url": "https://s3.eu-west-2.amazonaws.com/...",
  "expires_in": 900,
  "promoted": true
}
```

**Key field: `presigned_url`** — a download URL valid for **900 seconds** (15 minutes). Mint-on-read: do not cache beyond expiry.

**Errors:**
- 400 — missing `site_id`, `camera_id`, or `snapshot_id`
- 403 — caller lacks access to the site
- 404 — snapshot does not exist, or has expired under the 7-day out-of-hours retention and was never promoted

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

## Timelapse Jobs

A timelapse job renders a camera's snapshots over a date range into an MP4. The
flow is: **submit** a job (`POST`), **poll** its status (`GET .../{job_id}`) or
**list** jobs (`GET`), and once a job is `complete` use the presigned
`download_url` to fetch the video.

Job lifecycle `status` is one of: `queued`, `processing`, `complete`, `failed`.

Job records and their rendered artifacts are retained for **30 days**, then
expire together — a listed `complete` job whose artifact has since expired
reports `artifact_available: false` instead of a broken link (see below).

---

### POST /v1/timelapse-jobs

Submits a render job. It is created `queued` and processed asynchronously. Any
user with access to the site can submit.

**Query params:**
- `tenant_id` (required for super admins only)

**Body:**
```json
{
  "site_id": "site_001",
  "camera_id": "cam_01",
  "start": "2025-06-01T00:00:00Z",
  "end": "2025-06-07T23:59:59Z",
  "length_seconds": 60,
  "fps": 24
}
```

**Field rules:**
- `site_id` — required
- `camera_id` — required
- `start` — required, ISO8601 date or datetime. Date-only (`2025-06-01`) expands to start-of-day (`T00:00:00Z`).
- `end` — required, ISO8601 date or datetime. Date-only expands to end-of-day (`T23:59:59Z`). Must be strictly after `start`.
- `length_seconds` — optional, integer 1–120, default 60 (target output duration)
- `fps` — optional, integer 1–30, default 24

**Response (202):**
```json
{
  "job_id": "b3f1c2a4-...",
  "status": "queued"
}
```

Poll `GET /v1/timelapse-jobs/{job_id}` with the returned `job_id` to track progress.

**Errors:**
- 400 — missing/blank required field, invalid date/datetime format, `end` not after `start`, `length_seconds`/`fps` out of range, or super admin missing `tenant_id`
- 403 — caller lacks access to the site
- 404 — site does not exist, or no footage exists in the requested range
- 500 — persistence or enqueue failure

---

### GET /v1/timelapse-jobs/{job_id}

Returns a single job's current status and, when complete, a freshly presigned
download URL.

**Query params:**
- `tenant_id` (required for super admins only)

**Response — queued / processing (200):**
```json
{
  "status": "processing",
  "requested_by": "jane.doe@acme.example.com",
  "completed_at": null
}
```

**Response — complete, artifact available (200):**
```json
{
  "status": "complete",
  "requested_by": "jane.doe@acme.example.com",
  "completed_at": "2025-06-08T09:17:22Z",
  "download_url": "https://s3.eu-west-2.amazonaws.com/...",
  "expires_in": 3600
}
```

**Response — complete, artifact expired/gone (200):**
```json
{
  "status": "complete",
  "requested_by": "jane.doe@acme.example.com",
  "completed_at": "2025-06-08T09:17:22Z",
  "artifact_available": false
}
```

**Response — failed (200):**
```json
{
  "status": "failed",
  "requested_by": "jane.doe@acme.example.com",
  "completed_at": null,
  "reason": "No frames could be decoded in the requested range."
}
```

**Field notes:**
- `requested_by` — the submitter (JWT `sub`, falling back to email), or `null` if neither was present.
- `completed_at` — ISO8601 UTC timestamp, non-null only when `status` is `complete`.
- `download_url` / `expires_in` — present only for a `complete` job whose artifact still exists. `expires_in` is always 3600 (1 hour). Mint-on-read: do not cache beyond expiry.
- `artifact_available: false` — present only for a `complete` job whose artifact has expired. Show a "no longer available" state, not a broken link.
- `reason` — present only when `status` is `failed`.

**Errors:**
- 400 — missing `job_id`, or super admin missing `tenant_id`
- 404 — job does not exist, **or** the caller is not authorized for it (existence is deliberately not leaked, so treat 404 as "not available to you")
- 500 — lookup failure

---

### GET /v1/timelapse-jobs

Returns a paginated, filterable list of the tenant's jobs, newest-first. Use it
for an "all renders" view (tenant-wide) or a "renders for this camera" view
(with `site_id` + `camera_id`).

**Query params:**
- `site_id` (optional filter)
- `camera_id` (optional filter — **requires** `site_id`)
- `status` (optional filter — one of `queued`, `processing`, `complete`, `failed`)
- `limit` (optional, 1–100, default 20)
- `cursor` (optional, opaque token from a previous response)
- `tenant_id` (required for super admins only)

Filters are exact, case-sensitive, and AND-combined.

**Response (200):**
```json
{
  "jobs": [
    {
      "job_id": "b3f1c2a4-...",
      "site_id": "site_001",
      "camera_id": "cam_01",
      "start": "2025-06-01T00:00:00Z",
      "end": "2025-06-07T23:59:59Z",
      "length_seconds": 60,
      "status": "complete",
      "created_at": "2025-06-08T09:15:00Z",
      "completed_at": "2025-06-08T09:17:22Z",
      "requested_by": "jane.doe@acme.example.com",
      "download_url": "https://s3.eu-west-2.amazonaws.com/...",
      "expires_in": 3600
    }
  ],
  "next_cursor": "eyJQSyI6IC4uLn0="
}
```

**Ordering:** newest-first by `created_at` descending, with `job_id` descending as a stable tie-break.

**Per-entry fields:** same download/availability rules as the single-job endpoint — `download_url` + `expires_in` (3600) appear only for a `complete` job with an existing artifact; `artifact_available: false` appears only for a `complete` job whose artifact has expired; `completed_at` is `null` unless `complete`; `requested_by` is `null` when it was never captured.

**Pagination:** pass `next_cursor` back as `?cursor=<value>`. `null` means the last page. Treat cursors as opaque.

**Scope by role:**
- **Super admin:** all jobs in the specified `tenant_id`
- **Tenant admin:** all jobs in their tenant
- **User:** only jobs whose `site_id` is in their `custom:site_access`. A supplied `site_id` outside their access, or an empty access list, returns `200` with an empty `jobs` array and `next_cursor: null` (not an error).

**Errors:**
- 400 — blank `site_id`/`camera_id`/`status`, `camera_id` without `site_id`, invalid `status`, `limit` outside 1–100 / non-integer, invalid `cursor`, or super admin missing `tenant_id`
- 403 — tenant admin / user with no resolvable tenant
- 500 — DynamoDB or S3 failure (no partial `jobs` returned)

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
- Methods: `GET, POST, PUT, PATCH, DELETE, OPTIONS`
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

### PATCH /v1/sites/{site_id}/cameras/{camera_id}

Updates a camera's editable metadata. Use this to rename a camera or change its model. **Tenant admin or super admin.** (Tenant admins can only edit cameras within their own tenant.)

`camera_id` and the ingest token are immutable and cannot be changed here.

**Query params:**
- `tenant_id` (required for super admins; tenant admins use their own tenant from JWT)

**Body — at least one field required:**
```json
{
  "camera_name": "South gate",
  "camera_model": "Axis Q6135-LE"
}
```

**Field rules:**
- `camera_name` — optional; when present must be a non-empty string (max 120 chars)
- `camera_model` — optional; a non-empty string (max 120 chars), or `null` to clear it
- At least one of `camera_name` / `camera_model` must be present

**Response (200):**
```json
{
  "camera_id": "cam_01",
  "site_id": "site_001",
  "tenant_id": "acme_corp",
  "camera_name": "South gate",
  "camera_model": "Axis Q6135-LE"
}
```

The response reflects the camera's state after the update. `camera_model` is omitted if the camera has no model set.

**Errors:**
- 400 — empty body, invalid/empty `camera_name`, invalid `camera_model`, or super admin missing tenant_id query param
- 403 — caller is a regular user, or tenant admin editing another tenant's camera
- 404 — camera does not exist

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

Updates site configuration. Supports setting `working_hours` (days of week + time window), plus `latitude`, `longitude`, and `timezone`. **Tenant admin or super admin.**

> **Renamed from `ingest_hours`.** The old `ingest_hours` field has been replaced by `working_hours`, which adds a `days` array. Sending `ingest_hours` now returns `400 BAD_REQUEST`. See the migration note below.

**Query params:**
- `tenant_id` (required for super admins; tenant admins use their own tenant from JWT)

**Body — set working hours:**
```json
{
  "working_hours": {
    "days": ["mon", "tue", "wed", "thu", "fri"],
    "start": "07:00",
    "end": "18:00"
  }
}
```

**Body — clear working hours (treat all snapshots as in-hours):**
```json
{
  "working_hours": null
}
```

**Field rules:**
- `working_hours` — an object with `start`, `end`, and optional `days`, or `null` to clear
- `start` — required, HH:MM 24-hour format in the range `00:00`–`23:59`
- `end` — required, HH:MM 24-hour format in the range `00:00`–`23:59`
- `days` — optional, a list of 1–7 entries drawn from the lowercase set `{mon, tue, wed, thu, fri, sat, sun}`, with no duplicates. Entries must be exact-lowercase (`"Mon"` is rejected).
- When `days` is omitted, it defaults to all seven days
- Supports overnight windows (e.g. `start: "22:00"`, `end: "06:00"`), anchored to the day on which the window begins
- Times are interpreted in the site's configured timezone
- You may also update `latitude` (float, -90–90), `longitude` (float, -180–180), and `timezone` (valid IANA identifier) in the same request

**Response (200):**
```json
{
  "site_id": "site_001",
  "tenant_id": "acme_corp",
  "working_hours": {
    "days": ["mon", "tue", "wed", "thu", "fri"],
    "start": "07:00",
    "end": "18:00"
  }
}
```

The response echoes only the fields that were updated. When `working_hours` is cleared with `null`, the field is omitted from the response.

**Behaviour — 24/7 ingestion with retention classes:** Snapshots are now saved **24 hours a day**, at the existing 15-minute cadence — working hours no longer decide *whether* an image is saved, only how long it is retained:
- **In-hours** (captured within `working_hours`, evaluated in the site timezone) — long-term timelapse retention. These appear in `GET /v1/snapshots` and `GET /v1/snapshots/latest`.
- **Out-of-hours** (captured outside `working_hours`) — a fixed **7-day** expiry, after which they are auto-deleted unless promoted. These are excluded from the default list/latest views and are accessed via the out-of-hours endpoints (review / promote / download) documented below.

When `working_hours` is `null`, every snapshot is treated as in-hours. The fixed 7-day out-of-hours retention is **not configurable** — attempting to set any out-of-hours TTL field returns `400 BAD_REQUEST`.

**Migration note:** Existing sites still stored with the legacy `ingest_hours` attribute keep working — the API reads them transparently and surfaces them as `working_hours` with `days` defaulted to all seven days. The next successful `working_hours` write removes the legacy attribute. Clients should send and read `working_hours` only.

**Errors:**
- 400 — `ingest_hours` field sent (use `working_hours`), invalid `start`/`end` format or range, missing `start`/`end`, invalid `days` (empty, >7, duplicate, unknown, or wrong case), attempt to configure the out-of-hours TTL, or super admin missing `tenant_id`
- 403 — caller is a regular user
- 404 — site does not exist

---

### PUT /v1/tenants/{tenant_id}/logo

Uploads or replaces a tenant's company logo. **Super admin only.**

**Content-Type:** Must be `image/jpeg` or `image/png` (sent as the request header, not JSON).

**Body:** Raw binary image data (not JSON — this is a file upload).

**Constraints:**
- Maximum file size: 2 MB
- Accepted formats: JPEG, PNG

**Example (using axios):**
```typescript
async function uploadLogo(tenantId: string, file: File) {
  const buffer = await file.arrayBuffer();

  return api.put(`/v1/tenants/${tenantId}/logo`, buffer, {
    headers: {
      'Content-Type': file.type, // 'image/jpeg' or 'image/png'
    },
  });
}
```

**Response (200):**
```json
{
  "tenant_id": "acme_corp",
  "logo_key": "logos/acme_corp/logo.png",
  "content_type": "image/png",
  "size_bytes": 48291
}
```

**Displaying the logo:** Use the GET endpoint below to retrieve a presigned URL, then use it as an `<img src>`.

**Errors:**
- 400 — unsupported Content-Type, body empty, file exceeds 2 MB
- 403 — caller is not super_admin
- 404 — tenant does not exist

---

### GET /v1/tenants/{tenant_id}/logo

Returns a presigned S3 URL for the tenant's logo image. **Any authenticated user.**

Use the returned `presigned_url` directly as `<img src="...">` — no additional auth needed on the image request.

**Response (200):**
```json
{
  "tenant_id": "acme_corp",
  "presigned_url": "https://s3.eu-west-2.amazonaws.com/sitespy-dev-snapshots-.../logos/acme_corp/logo.png?X-Amz-...",
  "expires_in": 3600
}
```

**Key field: `presigned_url`** — temporary S3 URL valid for 1 hour (3600 seconds). Use directly as an `<img src>`. Re-fetch when expired.

**Key field: `expires_in`** — always 3600. You can cache the URL in memory for up to this duration.

**Example (displaying in React):**
```typescript
const { data } = await api.get(`/v1/tenants/${tenantId}/logo`);
// Use data.presigned_url as <img src={data.presigned_url} alt="Company logo" />
```

**Errors:**
- 400 — missing tenant_id path parameter
- 404 — tenant does not exist, or no logo has been uploaded

---

## What the API Does NOT Provide (Yet)

These endpoints are planned for future phases:

- `POST /v1/exports` — bulk image export

The dashboard should be designed with these features in mind but they can be stubbed initially.

> Timelapse generation has shipped — see the **Timelapse Jobs** section above (`POST /v1/timelapse-jobs`, `GET /v1/timelapse-jobs/{job_id}`, `GET /v1/timelapse-jobs`).

---

## Camera Transfer (Super Admin)

### POST /v1/cameras/transfer

Moves a camera from one tenant/site to another. Designed for the camera staging workflow: cameras are provisioned and tested in the hidden `sandbox_construction` tenant, then transferred to the customer's tenant once verified. **Super admin only.**

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

**Response (200):**
```json
{
  "tenant_id": "acme_corp",
  "site_id": "site_001",
  "camera_id": "cam_01"
}
```

**What happens on transfer:**
- The camera record moves to the target tenant/site
- The ingest token is preserved — the physical camera keeps working without reconfiguration
- Historical snapshots stay in the source tenant (they're test data)
- The camera immediately appears in the target site's camera list

**Errors:**
| Code | Error Key | Meaning | Frontend Action |
|------|-----------|---------|----------------|
| 400 | `BAD_REQUEST` | Missing/empty required field | Show which field is invalid (from `message`) |
| 403 | `ACCESS_DENIED` | Not super_admin | Show access denied |
| 404 | `NOT_FOUND` | Source camera, target tenant, or target site not found | Show error toast with the `message` |
| 409 | `CONFLICT` | Camera already exists at target | Show "camera already exists at this site" |
| 500 | `INTERNAL_ERROR` | Transaction failure | Show generic error with retry |

**Suggested UX:** A "Transfer Camera" dialog in the super admin view for the sandbox tenant. The dialog shows a target tenant picker, then a site picker within that tenant. On success, the camera disappears from the sandbox view and appears in the customer's site.

---

## Sandbox Tenant (Super Admin Only)

The system has a hidden tenant `sandbox_construction` ("Sandbox Construction") used for camera staging. Key points for the frontend:

1. **Not visible to customers.** The sandbox tenant never appears in `GET /v1/tenants` for non-super_admin roles. No frontend filtering needed — the API handles it.
2. **403 for non-super_admins.** If any non-super_admin user somehow navigates to a URL referencing `sandbox_construction`, all API calls will return `403 ACCESS_DENIED`. Handle this like any other access denied response.
3. **Super admins see it normally.** For super admins, the sandbox tenant appears in the tenant list and works like any other tenant — you can view sites, cameras, and snapshots within it.
4. **Transfer button.** The primary action on sandbox cameras is "Transfer to customer." This calls `POST /v1/cameras/transfer` (above).
