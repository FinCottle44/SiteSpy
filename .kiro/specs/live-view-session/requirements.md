# Requirements Document

## Introduction

This document covers two related changes to the SiteSpy ingest and data-plane:

**Part 1 — Timelapse cadence filtering** introduces a 15-minute minimum gap between
timelapse snapshots for each camera. The Ingest_Handler already accepts every push from
the camera (every 1 minute); this change makes it silently discard pushes that arrive
too soon after the last saved snapshot, protecting timelapse integrity without altering
the camera configuration.

**Part 2 — Live view sessions** lets authenticated users request a temporary high-frequency
feed for a specific camera. While a session is active, every 1-minute push is written to a
separate `live/` S3 prefix and a separate `LIVE_IMG#` DynamoDB record type. Live images do
not count as timelapse snapshots and do not interact with the 15-minute cadence check. A
session expires after 10 minutes (or on explicit deletion), after which live images are
cleaned up automatically via S3 Lifecycle and DynamoDB TTL.

A comprehensive frontend handover document (`docs/live_view_handover.md`) covering all new
endpoints, request/response shapes, polling patterns, session lifecycle, and error handling
must be produced as part of this feature.

---

## Glossary

- **Ingest_Handler**: The Lambda function behind `POST /v1/ingest/{token}`.
- **Session_Handler**: The Lambda function(s) behind the three live-session API routes.
- **Camera_Record**: The DynamoDB item keyed `(TENANT#<id>, SITE#<site>#CAM#<cam>)` representing a provisioned camera.
- **IMG_Record**: A DynamoDB item whose SK begins with `IMG#`, recording one timelapse snapshot.
- **LIVE_IMG_Record**: A DynamoDB item whose SK begins with `LIVE_IMG#`, recording one live-view snapshot.
- **Session_Record**: A DynamoDB item whose SK begins with `SESSION#`, representing one live-view session.
- **Cadence_Check**: The per-push decision in the Ingest_Handler that determines whether an incoming push should be saved as a timelapse snapshot.
- **Live_Session**: A time-bounded activation that causes the Ingest_Handler to write live snapshots for a specific camera.
- **Session_ID**: An opaque identifier (ULID or UUID v4) that uniquely identifies one Live_Session within a tenant.
- **Timelapse_Prefix**: The existing S3 key structure `<tenant>/<site>/<camera>/<YYYY>/<MM>/<DD>/<ts>.jpg`.
- **Live_Prefix**: The S3 key structure `live/<tenant>/<site>/<camera>/<ts>.jpg` used exclusively for live-view snapshots.
- **expires_at**: An ISO 8601 UTC timestamp string marking the moment a Live_Session becomes inactive.
- **TTL**: DynamoDB Time-to-Live attribute (`ttl`), stored as a Unix epoch integer, used for automatic record expiry.
- **Presigned_URL**: A temporary S3 GET URL with a fixed validity window (300 seconds), returned by the API to the frontend.
- **Super_Admin**: A user in the `SuperAdmins` Cognito group with platform-wide access.
- **Tenant_Admin**: A user in the `TenantAdmins` Cognito group with tenant-scoped access.
- **User**: An authenticated user with no admin group membership, scoped to specific sites via `custom:site_access`.

---

## Requirements

### Requirement 1: Timelapse Cadence Filtering

**User Story:** As a SiteSpy operator, I want the ingest pipeline to enforce a minimum 15-minute gap between saved timelapse snapshots, so that the timelapse image sequence maintains a consistent cadence regardless of how frequently the camera pushes.

#### Acceptance Criteria

1. WHEN a push arrives at `POST /v1/ingest/{token}`, THE Ingest_Handler SHALL query DynamoDB for the most recent IMG_Record for that camera before deciding whether to save the image.
2. IF the most recent IMG_Record for a camera has an `ingested_at` timestamp less than 15 minutes before the current UTC time, THEN THE Ingest_Handler SHALL accept the request and return HTTP 200 without writing to S3 or DynamoDB.
3. IF the most recent IMG_Record for a camera has an `ingested_at` timestamp 15 minutes or more before the current UTC time, THEN THE Ingest_Handler SHALL save the snapshot to the Timelapse_Prefix in S3, write an IMG_Record to DynamoDB, and return HTTP 201.
4. IF no IMG_Record exists for a camera (first push ever), THEN THE Ingest_Handler SHALL save the snapshot unconditionally, bypassing both the ingest-hours check and the Cadence_Check.
5. IF a push is discarded by the Cadence_Check, THEN THE Ingest_Handler SHALL return HTTP 200 with `{"status": "skipped", "reason": "cadence_filter", "camera_id": "<id>"}` so the camera continues to push without error.
6. THE Cadence_Check SHALL be evaluated after the ingest-hours check — pushes already discarded by the ingest-hours filter SHALL NOT be re-evaluated by the Cadence_Check.
7. IF the DynamoDB query for the latest IMG_Record fails, THEN THE Ingest_Handler SHALL log the error, treat the result as no prior record (fail open), and proceed to write the snapshot as if it were the first push, returning HTTP 201.
8. THE Cadence_Check SHALL use only the `ingested_at` timestamp of the most recent IMG_Record — it SHALL NOT be affected by the presence or absence of LIVE_IMG_Records.
9. FOR ALL cameras where the Cadence_Check passes on consecutive pushes, the gap between `ingested_at` values of consecutive IMG_Records SHALL be at least 14 minutes and 50 seconds, accounting for sub-second Lambda execution variance.

### Requirement 2: Live Session Creation

**User Story:** As an authenticated user with site access, I want to start a live view session for a specific camera, so that I can see near-real-time images updating every minute for 10 minutes.

#### Acceptance Criteria

1. WHEN `POST /v1/sites/{site_id}/cameras/{camera_id}/live-session` is called by an authenticated User with access to `site_id`, THE Session_Handler SHALL create a Session_Record in DynamoDB and return HTTP 201 with `session_id`, `expires_at`, and `camera_id`.
2. WHEN `POST /v1/sites/{site_id}/cameras/{camera_id}/live-session` is called by an authenticated Tenant_Admin, THE Session_Handler SHALL create the session for any camera within the caller's tenant without requiring explicit site-access membership.
3. WHEN `POST /v1/sites/{site_id}/cameras/{camera_id}/live-session` is called by a Super_Admin, THE Session_Handler SHALL require a `tenant_id` query parameter and create the session for the specified camera in that tenant.
4. THE Session_Handler SHALL set `expires_at` to exactly 10 minutes after the moment the Session_Record DynamoDB write succeeds, expressed as an ISO 8601 string ending in `Z`.
5. THE Session_Handler SHALL set the DynamoDB `ttl` attribute on the Session_Record to a Unix epoch integer equal to `expires_at + 3600` (1 hour after session expiry), retaining the record for diagnostics before DynamoDB TTL removes it.
6. WHEN a Live_Session already exists for the specified camera at the time of the `POST`, THE Session_Handler SHALL return HTTP 409 with error key `SESSION_ALREADY_ACTIVE` rather than creating a second session.
7. IF the session existence check fails due to a DynamoDB error, THE Session_Handler SHALL return HTTP 500 with error key `INTERNAL_ERROR`.
8. WHEN the specified `camera_id` does not exist within the resolved tenant and site, THE Session_Handler SHALL return HTTP 404 with error key `NOT_FOUND`.
9. IF the caller is a User and the `site_id` is not in the caller's `custom:site_access` claim, THEN THE Session_Handler SHALL return HTTP 403 with error key `ACCESS_DENIED`.
10. IF the caller is a Super_Admin and the `tenant_id` query parameter is absent or refers to a non-existent tenant, THEN THE Session_Handler SHALL return HTTP 400 with error key `BAD_REQUEST`.
11. THE Session_Handler SHALL generate a `session_id` that is unique across all sessions for a tenant using UUID v4.
12. WHEN the DynamoDB Session_Record write itself fails, THE Session_Handler SHALL return HTTP 500 with error key `INTERNAL_ERROR` without returning a session to the caller.
13. THE Session_Record SHALL be stored with:
    - `PK = TENANT#<tenant_id>`
    - `SK = SESSION#<site_id>#<camera_id>`
    - `session_id = <uuid-v4>`
    - `expires_at = <ISO 8601 UTC string>`
    - `ttl = <unix epoch integer>`
    - `created_by = <caller sub from JWT>`
    - `created_at = <ISO 8601 UTC string>`

### Requirement 3: Live Session Querying

**User Story:** As an authenticated user with site access, I want to poll the live session status endpoint every 15 seconds to get the latest live image and a countdown to session expiry, so that the frontend can display a near-real-time feed.

#### Acceptance Criteria

1. WHEN `GET /v1/sites/{site_id}/cameras/{camera_id}/live-session` is called by an authorised caller and an active Session_Record exists for the camera, THE Session_Handler SHALL return HTTP 200 with `session_id`, `expires_at`, `status: "active"`, and a `latest_image` object containing `presigned_url` and `captured_at` of the most recent LIVE_IMG_Record.
2. WHEN `GET /v1/sites/{site_id}/cameras/{camera_id}/live-session` is called by an authorised caller and no active Session_Record exists for the camera (never started or already expired/deleted), THE Session_Handler SHALL return HTTP 200 with `{"status": "none"}` and no `session_id`, `expires_at`, or `latest_image` fields.
3. WHEN `GET /v1/sites/{site_id}/cameras/{camera_id}/live-session` is called by an authorised caller and an active Session_Record exists but no LIVE_IMG_Record has been written yet, THE Session_Handler SHALL return HTTP 200 with `status: "active"`, `session_id`, `expires_at`, and `latest_image: null`.
4. THE Session_Handler SHALL determine active status by comparing `expires_at` from the Session_Record against the current UTC time — a session whose `expires_at` is in the past SHALL be treated as inactive even if the DynamoDB record has not yet been removed by TTL.
5. THE `presigned_url` within the `latest_image` response object SHALL have an S3 expiry of 300 seconds.
6. THE Session_Handler SHALL enforce the same role-based access rules for GET as for POST (Requirement 2, criteria 2, 3, 9, 10), validating authorisation before checking session status.
7. WHEN the DynamoDB query for the most recent LIVE_IMG_Record fails, THE Session_Handler SHALL log the error and return HTTP 200 with `session_id`, `expires_at`, and `status` intact, omitting the `latest_image` field entirely.
8. WHEN the DynamoDB query for the Session_Record itself fails, THE Session_Handler SHALL return HTTP 500 with error key `INTERNAL_ERROR`.

### Requirement 4: Live Session Deletion

**User Story:** As an authenticated user with site access, I want to end a live view session before its natural expiry, so that I can stop accumulating live images when I no longer need the feed.

#### Acceptance Criteria

1. WHEN `DELETE /v1/sites/{site_id}/cameras/{camera_id}/live-session` is called by an authorised caller and an active Session_Record exists, THE Session_Handler SHALL delete the Session_Record from DynamoDB and return HTTP 200 with `{"status": "deleted"}`.
2. IF the DynamoDB deletion operation fails, THEN THE Session_Handler SHALL return HTTP 500 with error key `INTERNAL_ERROR`.
3. WHEN `DELETE /v1/sites/{site_id}/cameras/{camera_id}/live-session` is called and no active Session_Record exists (or the session has already expired), THE Session_Handler SHALL return HTTP 404 with error key `NOT_FOUND`.
4. A subsequent `POST` to create a new session for the same camera after a successful DELETE SHALL succeed normally.
5. THE Session_Handler SHALL enforce the same role-based access rules for DELETE as for POST (Requirement 2, criteria 2, 3, 9, 10), validating authorisation before checking session existence.
6. WHEN the Session_Record is deleted via `DELETE`, THE Session_Handler SHALL NOT delete LIVE_IMG_Records or S3 objects — cleanup SHALL be handled exclusively by S3 Lifecycle rules and DynamoDB TTL.

### Requirement 5: Live Snapshot Ingestion

**User Story:** As a SiteSpy operator, I want the ingest pipeline to write a separate live-view snapshot for every camera push that occurs while a live session is active, so that the frontend polling loop can display near-real-time images.

#### Acceptance Criteria

1. WHEN a push arrives at `POST /v1/ingest/{token}` and an active Session_Record exists for the camera, THE Ingest_Handler SHALL write the JPEG to the Live_Prefix in S3 and write a LIVE_IMG_Record to DynamoDB, in addition to (or instead of) any timelapse write determined by the Cadence_Check.
2. THE Live_Prefix key for a live snapshot SHALL follow the format `live/<tenant_id>/<site_id>/<camera_id>/<snapshot_ts>.jpg`.
3. THE Ingest_Handler SHALL NOT use the presence or absence of a live session to alter the Cadence_Check result — the timelapse save decision SHALL be made independently of the live session state.
4. WHEN both a timelapse write and a live write are required for the same push, THE Ingest_Handler SHALL perform both writes.
5. WHEN only a live write is required (timelapse suppressed by Cadence_Check), THE Ingest_Handler SHALL write only the LIVE_IMG_Record and live S3 object.
6. THE LIVE_IMG_Record SHALL be stored with:
    - `PK = TENANT#<tenant_id>`
    - `SK = LIVE_IMG#<site_id>#<camera_id>#<snapshot_ts>`
    - `s3_key = <live_prefix_key>`
    - `sha256 = <hex digest>`
    - `size_bytes = <integer>`
    - `captured_at = <snapshot_ts>`
    - `ttl = <unix epoch integer equal to captured_at + 3600>`
7. THE Ingest_Handler SHALL look up the active session by querying DynamoDB for `PK = TENANT#<tenant_id>` and `SK = SESSION#<site_id>#<camera_id>`, checking that the returned record has `expires_at` in the future.
8. WHEN the DynamoDB session lookup fails during ingest, THE Ingest_Handler SHALL log the error, treat the session as absent (no live write), and continue with the normal timelapse logic respecting the Cadence_Check result — if the Cadence_Check would have suppressed the timelapse write, it still SHALL be suppressed.
9. WHEN the S3 live write fails, THE Ingest_Handler SHALL log the error and return HTTP 500; a partial write (S3 success but DynamoDB LIVE_IMG_Record failure, or vice versa) SHALL also result in HTTP 500.
10. THE Ingest_Handler SHALL return the normal HTTP 201 response body when both a timelapse write and a live write succeed; it SHALL include a `live_captured: true` field in the response body to indicate a live snapshot was also written.
11. WHEN only a live write occurs (Cadence_Check suppressed the timelapse), THE Ingest_Handler SHALL return HTTP 200 with `{"status": "skipped", "reason": "cadence_filter", "camera_id": "<id>", "live_captured": true}`.

### Requirement 6: Live Image Cleanup

**User Story:** As a SiteSpy operator, I want live view images and session records to be cleaned up automatically after expiry, so that the system does not accumulate indefinite storage from live sessions without manual intervention.

#### Acceptance Criteria

1. THE SnapshotsBucket S3 Lifecycle configuration SHALL include a rule that expires all objects with key prefix `live/` after 1 hour (3600 seconds from object creation).
2. THE DataTable DynamoDB TTL configuration SHALL be enabled on the `ttl` attribute, causing LIVE_IMG_Records and expired Session_Records to be automatically deleted by DynamoDB within 48 hours of their `ttl` timestamp.
3. THE Session_Handler and Ingest_Handler SHALL NOT implement any Lambda-based cleanup logic for live images or session records; all cleanup SHALL be handled by the S3 Lifecycle rule and DynamoDB TTL.
4. THE `ttl` attribute on LIVE_IMG_Records SHALL be set to the Unix epoch integer value of `captured_at + 3600` (1 hour after capture).
5. THE `ttl` attribute on Session_Records SHALL be set to the Unix epoch integer value of `expires_at + 3600` (1 hour after session expiry).

### Requirement 7: Session API Authentication and Authorisation

**User Story:** As a security-conscious operator, I want the live session endpoints to enforce the same role-based access controls as the rest of the SiteSpy API, so that only users with appropriate permissions can start, view, or end live sessions.

#### Acceptance Criteria

1. THE Session_Handler SHALL require a valid Cognito ID token in the `Authorization: Bearer <token>` header for all three live-session routes; missing or invalid tokens SHALL return HTTP 401 with error key `UNAUTHORIZED`; this check SHALL occur before any authorisation or session-existence checks.
2. IF the caller is a User (no Cognito group), THEN THE Session_Handler SHALL verify that the `site_id` path parameter appears in the caller's `custom:site_access` JWT claim; this check SHALL occur only after authentication has been confirmed, and SHALL return HTTP 403 with error key `ACCESS_DENIED` on failure.
3. IF the caller is a Tenant_Admin, THEN THE Session_Handler SHALL resolve the tenant from the caller's `custom:tenant_id` JWT claim and permit access to all cameras within that tenant without requiring `custom:site_access` entries.
4. IF the caller is a Super_Admin, THEN THE Session_Handler SHALL require a `tenant_id` query parameter and use it as the resolved tenant; requests without `tenant_id` SHALL return HTTP 400 with error key `BAD_REQUEST`.
5. THE Session_Handler SHALL return HTTP 403 with error key `ACCESS_DENIED` when a User attempts to access a `site_id` not in their `custom:site_access` claim; IF the caller is also unauthenticated, HTTP 401 SHALL take precedence over HTTP 403.
6. IF the caller is a Tenant_Admin and attempts to access a camera outside their own tenant, THEN THE Session_Handler SHALL return HTTP 403 with error key `ACCESS_DENIED`; this check SHALL occur only after authentication has been confirmed.

### Requirement 8: Frontend Handover Documentation

**User Story:** As a frontend developer, I want a complete handover document for the live view session feature that follows the same style as the existing `docs/api_handover.md`, so that I can integrate the feature without needing to read the backend source code.

#### Acceptance Criteria

1. THE live_view_handover document SHALL be created at `docs/live_view_handover.md` and SHALL document all three live-session API routes (`POST`, `GET`, `DELETE /v1/sites/{site_id}/cameras/{camera_id}/live-session`) with request parameters, example request bodies, and full example response bodies for each state.
2. THE live_view_handover document SHALL describe the recommended frontend polling pattern using a 15-second poll interval, how to display the countdown to `expires_at`, and how to handle the `status: "none"` and `status: "active"` states.
3. THE live_view_handover document SHALL document the pre-signed URL lifecycle (300-second expiry), the recommended strategy for refreshing images starting 60 seconds before URL expiry, and the expected behaviour when a URL has expired (S3 returns 403).
4. THE live_view_handover document SHALL describe all error codes returned by the live-session endpoints with the corresponding frontend action for each error (using the same table format as `docs/api_handover.md`).
5. THE live_view_handover document SHALL explain the complete session lifecycle: how a session starts, how it advances through the active state, how it expires naturally, how it can be ended early via DELETE, and what the frontend should display at each stage.
6. THE live_view_handover document SHALL include a TypeScript code example showing a complete session polling loop (start session, poll GET every 15 seconds, display countdown, handle session expiry/deletion).
7. THE live_view_handover document SHALL document the change to the ingest response: the new `live_captured` boolean field that indicates a live snapshot was written alongside (or instead of) a timelapse snapshot.
