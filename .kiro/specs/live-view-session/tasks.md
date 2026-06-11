# Implementation Plan: Live View Session

## Overview

Implement two features on top of the existing ingest pipeline:

1. **Timelapse cadence filtering** — add a 15-minute minimum gap check to `ingest.py` so that pushes arriving too soon are accepted but not written to S3/DynamoDB.
2. **Live view sessions** — add `SESSION#` and `LIVE_IMG#` DynamoDB record types, a new `live_session.py` handler (POST/GET/DELETE), corresponding data/storage helpers, SAM infrastructure changes, and a frontend handover document.

---

## Tasks

- [x] 1. Extend the data layer with SESSION# and LIVE_IMG# helpers
  - [x] 1.1 Add SESSION# key builders and CRUD functions to `src/sitespy/data.py`
    - Add `build_session_sk(site_id, camera_id) -> str` returning `SESSION#<site_id>#<camera_id>`
    - Add `get_live_session(tenant_id, site_id, camera_id) -> Mapping | None` — single `GetItem` by PK/SK
    - Add `put_live_session(tenant_id, site_id, camera_id, session_id, expires_at, ttl, created_by, created_at) -> None` — `PutItem` with `ConditionExpression: attribute_not_exists(SK)`; raises `ConditionalCheckFailedException` on duplicate
    - Add `delete_live_session(tenant_id, site_id, camera_id) -> None` — `DeleteItem` by PK/SK
    - _Requirements: 2.1, 2.4, 2.5, 2.6, 2.11, 2.13, 4.1, 6.2, 6.4, 6.5_

  - [x] 1.2 Add LIVE_IMG# key builders and record functions to `src/sitespy/data.py`
    - Add `build_live_img_sk(site_id, camera_id, snapshot_ts) -> str` returning `LIVE_IMG#<site_id>#<camera_id>#<snapshot_ts>`
    - Add `get_latest_live_img_record(tenant_id, site_id, camera_id) -> Mapping | None` — `Query` with `ScanIndexForward=False, Limit=1` on `LIVE_IMG#<site>#<cam>#` SK prefix
    - Add `put_live_img_record(tenant_id, site_id, camera_id, snapshot_ts, s3_key, sha256_hex, size_bytes, ttl) -> None` — unconditional `PutItem`
    - _Requirements: 3.1, 5.6, 6.4_

- [x] 2. Extend the storage layer with live-prefix helpers
  - [x] 2.1 Add `build_live_snapshot_key` and `put_live_snapshot` to `src/sitespy/storage.py`
    - Add `build_live_snapshot_key(tenant_id, site_id, camera_id, snapshot_ts) -> str` returning `live/<tenant_id>/<site_id>/<camera_id>/<snapshot_ts>.jpg`
    - Add `put_live_snapshot(key, body, sha256_hex, snapshot_ts, tenant_id) -> None` — `put_object` to the snapshots bucket with `ContentType=image/jpeg` and `Metadata`; no retention tag (lifecycle handles cleanup)
    - _Requirements: 5.2, 6.1_

- [x] 3. Checkpoint — unit tests for data and storage helpers
  - [x] 3.1 Write unit tests for SESSION# data functions
    - Test `build_session_sk` output format
    - Test `get_live_session` returns None when item absent; returns item when present
    - Test `put_live_session` raises `ConditionalCheckFailedException` on duplicate (mock DynamoDB)
    - Test `delete_live_session` calls `delete_item` with correct PK/SK
    - _Requirements: 2.1, 2.6, 2.13, 4.1_

  - [x] 3.2 Write unit tests for LIVE_IMG# data functions
    - Test `build_live_img_sk` output format
    - Test `get_latest_live_img_record` queries with correct `ScanIndexForward=False, Limit=1`
    - Test `put_live_img_record` writes correct TTL value (`captured_at + 3600`)
    - _Requirements: 3.1, 5.6, 6.4_

  - [x] 3.3 Write unit tests for storage helpers
    - Test `build_live_snapshot_key` produces `live/<tenant>/<site>/<cam>/<ts>.jpg`
    - Test `put_live_snapshot` does not include a retention tag
    - _Requirements: 5.2_

- [x] 4. Update the ingest handler with cadence filtering and live session writes
  - [x] 4.1 Add cadence check to `src/sitespy/handlers/ingest.py`
    - After the ingest-hours check, call `data.get_latest_img_record(tenant_id, site_id, camera_id)`
    - If the record exists and `ingested_at` is less than 15 minutes ago: set `save_timelapse = False`
    - If the record is absent, or `ingested_at` ≥ 15 minutes ago: set `save_timelapse = True`
    - On DynamoDB error: log the exception, set `save_timelapse = True` (fail open)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.6, 1.7, 1.8_

  - [x] 4.2 Add live session check and conditional live write to `src/sitespy/handlers/ingest.py`
    - After the cadence check, call `data.get_live_session(tenant_id, site_id, camera_id)`
    - If the record exists and `expires_at` > now: set `save_live = True`; otherwise `save_live = False`
    - On DynamoDB error: log and set `save_live = False` (fail open per requirement 5.8)
    - If `not save_timelapse and not save_live`: return 200 `{"status": "skipped", "reason": "cadence_filter", "camera_id": ..., "live_captured": false}`
    - If `save_timelapse`: write to timelapse S3 prefix + put `IMG#` record (existing code path)
    - If `save_live`: call `storage.put_live_snapshot` + `data.put_live_img_record`; on any error return 500
    - Add `live_captured: bool` to all 201 and cadence-skip 200 response bodies
    - _Requirements: 1.5, 5.1, 5.3, 5.4, 5.5, 5.7, 5.8, 5.9, 5.10, 5.11_

- [x] 5. Checkpoint — unit tests for the updated ingest handler
  - [x] 5.1 Write unit tests for the cadence check logic
    - Test: first push (no IMG# record) → `save_timelapse = True`
    - Test: last record < 15 min ago → `save_timelapse = False`
    - Test: last record ≥ 15 min ago → `save_timelapse = True`
    - Test: DynamoDB error → fail open (`save_timelapse = True`)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.7_

  - [x] 5.2 Write unit tests for the live session check and response shapes
    - Test: active session + timelapse saved → 201 with `live_captured: true`
    - Test: active session + cadence suppressed timelapse → 200 skipped with `live_captured: true`
    - Test: no active session + timelapse saved → 201 with `live_captured: false`
    - Test: no active session + cadence suppressed → 200 skipped with `live_captured: false`
    - Test: live S3 write failure → 500
    - _Requirements: 5.1, 5.3, 5.4, 5.5, 5.9, 5.10, 5.11_

- [x] 6. Implement the live session handler
  - [x] 6.1 Create `src/sitespy/handlers/live_session.py` with shared auth and response helpers
    - Add `handler_post`, `handler_get`, `handler_delete` as three Lambda entry points
    - Implement `_extract_claims`, `_resolve_caller`, `_check_access`, `_resolve_correlation_id` following the same pattern as `snapshots.py`
    - Implement tenant resolution (super_admin requires `?tenant_id=` query param → 400 if absent)
    - Add `error_response` / `json_response` imports from `sitespy.http`
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

  - [x] 6.2 Implement `_handle_post` — create a live session
    - Resolve tenant and authorise (call `_check_access`)
    - Verify camera exists via `data.get_camera(tenant_id, site_id, camera_id)` → 404 if absent
    - Call `data.get_live_session`; if active (exists and `expires_at` > now) → 409 `SESSION_ALREADY_ACTIVE`; DynamoDB error → 500
    - Compute `now`, `expires_at = now + 10 min`, `ttl = int(expires_at.timestamp()) + 3600`, `session_id = str(uuid4())`
    - Call `data.put_live_session` with `ConditionExpression`; `ConditionalCheckFailedException` → 409; other error → 500
    - Return 201 `{"session_id": ..., "expires_at": ..., "camera_id": ...}`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11, 2.12, 2.13_

  - [x] 6.3 Implement `_handle_get` — poll session status and latest live image
    - Resolve tenant and authorise
    - `data.get_live_session` → DynamoDB error → 500; not found or `expires_at` ≤ now → 200 `{"status": "none"}`
    - `data.get_latest_live_img_record` → DynamoDB error → log, return 200 with session fields, omit `latest_image`; no records → `latest_image: null`
    - `storage.generate_presigned_url(s3_key, expires_in=300)` for the live object
    - Return 200 `{"status": "active", "session_id": ..., "expires_at": ..., "latest_image": {"presigned_url": ..., "captured_at": ..., "expires_in": 300}}`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_

  - [x] 6.4 Implement `_handle_delete` — end a session early
    - Resolve tenant and authorise
    - `data.get_live_session` → DynamoDB error → 500; not found or `expires_at` ≤ now → 404 `NOT_FOUND`
    - `data.delete_live_session` → DynamoDB error → 500
    - Return 200 `{"status": "deleted"}`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

- [x] 7. Checkpoint — unit tests for the live session handler
  - [x] 7.1 Write unit tests for `handler_post`
    - Test: User with valid site access → 201
    - Test: User with missing site access → 403
    - Test: Super_Admin without `tenant_id` param → 400
    - Test: camera not found → 404
    - Test: session already active → 409
    - Test: `ConditionalCheckFailedException` on `put_live_session` → 409
    - Test: DynamoDB error on existence check → 500
    - _Requirements: 2.1, 2.6, 2.7, 2.8, 2.9, 2.10, 2.12_

  - [x] 7.2 Write unit tests for `handler_get`
    - Test: active session with live image → 200 with `presigned_url`
    - Test: active session, no live image yet → 200 `latest_image: null`
    - Test: no session record → 200 `{"status": "none"}`
    - Test: expired session record → 200 `{"status": "none"}`
    - Test: DynamoDB error on session lookup → 500
    - Test: DynamoDB error on live img query → 200 with session fields, no `latest_image`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.7, 3.8_

  - [x] 7.3 Write unit tests for `handler_delete`
    - Test: active session → 200 `{"status": "deleted"}`
    - Test: no active session → 404
    - Test: DynamoDB delete error → 500
    - _Requirements: 4.1, 4.2, 4.3_

- [x] 8. Add infrastructure changes to `template.yaml`
  - [x] 8.1 Enable DynamoDB TTL on the existing `DataTable`
    - Add `TimeToLiveSpecification: {AttributeName: ttl, Enabled: true}` to the `DataTable` resource
    - _Requirements: 6.2_

  - [x] 8.2 Add S3 Lifecycle rule for `live/` prefix to `SnapshotsBucket`
    - Add a new rule `Id: ExpireLiveSnapshotsAfter1Hour, Status: Enabled, Filter: {Prefix: live/}, Expiration: {Days: 1}` to the existing `LifecycleConfiguration`
    - _Requirements: 6.1_

  - [x] 8.3 Add `LiveSessionFunction` SAM resource with POST, GET, DELETE API events
    - Define `LiveSessionFunction` with three handler entry points dispatched from `live_session.handler_post`, `handler_get`, `handler_delete`
    - Policy: `DynamoDBCrudPolicy` on `DataTable` + `s3:GetObject` on `SnapshotsBucket/*`
    - Wire to `SiteSpyApi` at path `/v1/sites/{site_id}/cameras/{camera_id}/live-session` for POST, GET, DELETE
    - _Requirements: 2.1, 3.1, 4.1_

- [x] 9. Write the frontend handover document
  - [x] 9.1 Create `docs/live_view_handover.md`
    - Document all three live-session endpoints (POST, GET, DELETE) with request parameters and full example response bodies for every state
    - Describe the recommended 15-second polling pattern with countdown display, `status: "none"` and `status: "active"` handling
    - Document presigned URL lifecycle (300 s), refresh strategy (re-fetch 60 s before expiry), and expected 403 on expiry
    - Include error code table (same format as `docs/api_handover.md`) including `SESSION_ALREADY_ACTIVE`
    - Explain complete session lifecycle: start → active → natural expiry / early DELETE → cleanup
    - Include TypeScript polling loop example: start session, poll GET every 15 s, display countdown, handle expiry
    - Document the `live_captured` boolean field in ingest responses
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7_

- [x] 10. Final checkpoint
  - Ensure all tests pass. Ask the user if any questions arise before marking complete.

---

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP path.
- Each task references specific requirements for traceability.
- The ingest handler changes (task 4) must be backward-compatible — the existing 201 response fields (`key`, `timestamp`, `sha256`, `size_bytes`) are unchanged; `live_captured` is additive.
- DynamoDB TTL enables on the table affect new records only; existing records without a `ttl` attribute are unaffected.
- The `live/` S3 lifecycle minimum granularity is 1 day (`Days: 1`); exact 1-hour expiry is not supported by S3 Lifecycle natively — the DynamoDB TTL on `LIVE_IMG#` records provides the reference-level expiry within 48 hours.
- All new data layer functions follow the existing low-level DynamoDB client pattern (DynamoDB typed attributes, not resource style).

---

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "2.1"] },
    { "id": 1, "tasks": ["3.1", "3.2", "3.3", "4.1"] },
    { "id": 2, "tasks": ["4.2", "6.1"] },
    { "id": 3, "tasks": ["5.1", "5.2", "6.2", "6.3", "6.4"] },
    { "id": 4, "tasks": ["7.1", "7.2", "7.3", "8.1", "8.2", "8.3"] },
    { "id": 5, "tasks": ["9.1"] }
  ]
}
```
