# Design Document — Live View Session

## Overview

This feature adds two things to SiteSpy's ingest pipeline:

1. **Timelapse cadence filtering** — the ingest Lambda reads the latest `IMG#` record on every camera push and skips the timelapse write if fewer than 15 minutes have elapsed since the last saved snapshot. The camera always pushes every 1 minute; the backend decides what to do with each push.

2. **Live view sessions** — any authenticated user can start a 10-minute session for a specific camera. While a session is active, every 1-minute push is written to a separate `live/` S3 prefix and a `LIVE_IMG#` DynamoDB record. The frontend polls a new GET endpoint every 15 seconds to retrieve the latest live image and display it. Sessions expire automatically; S3 Lifecycle and DynamoDB TTL handle all cleanup with no Lambda involvement.

---

## Architecture

### Component Map

```
┌─────────────────────────────────────────────────────────────────────┐
│  Axis Camera (pushes every 1 min)                                   │
│  POST /v1/ingest/{token}                                            │
└─────────────────────┬───────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│  IngestFunction (Lambda)                                            │
│                                                                     │
│  1. Validate token → resolve tenant/site/camera                     │
│  2. Validate JPEG body                                              │
│  3. Check ingest_hours window                (existing)             │
│  4. Cadence check: read latest IMG# record   (NEW)                  │
│     └─ < 15 min? → skip timelapse write                             │
│     └─ ≥ 15 min or first push? → save timelapse                     │
│  5. Live session check: read SESSION# record (NEW)                  │
│     └─ active session? → write LIVE_IMG# + live/ S3 object          │
│  6. Return response                                                 │
└──────────┬──────────────────────────┬───────────────────────────────┘
           │                          │
           ▼                          ▼
  S3: timelapse prefix        S3: live/ prefix
  <tenant>/<site>/<cam>/...   live/<tenant>/<site>/<cam>/<ts>.jpg
           │                          │
           ▼                          ▼
  DynamoDB: IMG#              DynamoDB: LIVE_IMG#
  (permanent, no TTL)         (ttl = captured_at + 3600)

┌─────────────────────────────────────────────────────────────────────┐
│  LiveSessionFunction (Lambda) — NEW                                 │
│                                                                     │
│  POST   /v1/sites/{site_id}/cameras/{camera_id}/live-session        │
│  GET    /v1/sites/{site_id}/cameras/{camera_id}/live-session        │
│  DELETE /v1/sites/{site_id}/cameras/{camera_id}/live-session        │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
                  DynamoDB: SESSION#
                  (ttl = expires_at + 3600)

┌─────────────────────────────────────────────────────────────────────┐
│  S3 Lifecycle Rule (existing bucket, NEW rule)                      │
│  Prefix: live/  →  expire after 1 hour                              │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  DynamoDB TTL (existing table, NEW attribute)                       │
│  Attribute: ttl  →  auto-delete LIVE_IMG# and expired SESSION#       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## DynamoDB Schema Additions

The existing single table (`sitespy-{env}-data`) gains two new record types. No new table or GSI is required — both use `GetItem` by exact PK/SK, which is the cheapest access pattern.

### SESSION# Record

Stores the state of one live view session per camera. Only one record per `(tenant, site, camera)` tuple can exist at a time (enforced by a `ConditionExpression` on write).

| Attribute    | Type | Value |
|---|---|---|
| `PK`         | S    | `TENANT#<tenant_id>` |
| `SK`         | S    | `SESSION#<site_id>#<camera_id>` |
| `session_id` | S    | UUID v4 |
| `expires_at` | S    | ISO 8601 UTC, e.g. `2025-06-15T14:10:00Z` |
| `ttl`        | N    | Unix epoch of `expires_at + 3600` |
| `created_by` | S    | Cognito `sub` of the user who started the session |
| `created_at` | S    | ISO 8601 UTC timestamp of creation |

**Access patterns:**
- `GetItem` by `(PK, SK)` — used by ingest to check for active session, and by session GET/DELETE
- `PutItem` with `ConditionExpression: attribute_not_exists(SK)` — session POST (prevents duplicate)
- `DeleteItem` by `(PK, SK)` — session DELETE

### LIVE_IMG# Record

One record per live snapshot written. Keyed so that the most recent can be fetched with `Query ... ScanIndexForward=False, Limit=1`.

| Attribute    | Type | Value |
|---|---|---|
| `PK`         | S    | `TENANT#<tenant_id>` |
| `SK`         | S    | `LIVE_IMG#<site_id>#<camera_id>#<snapshot_ts>` |
| `s3_key`     | S    | `live/<tenant_id>/<site_id>/<camera_id>/<snapshot_ts>.jpg` |
| `sha256`     | S    | Hex SHA-256 of the JPEG body |
| `size_bytes` | N    | Byte length of the JPEG body |
| `captured_at`| S    | ISO 8601 UTC timestamp |
| `ttl`        | N    | Unix epoch of `captured_at + 3600` |

**Access patterns:**
- `Query PK=TENANT#... SK begins_with LIVE_IMG#<site>#<cam># ScanIndexForward=False Limit=1` — session GET (fetch latest)
- `PutItem` — ingest (write live snapshot)

> No GSI needed. All queries are on the main table's PK/SK.

### DynamoDB TTL

TTL must be enabled on the `ttl` attribute in `template.yaml`. This is a table-level setting — enabling it does not affect existing records that lack the attribute.

```yaml
TimeToLiveSpecification:
  AttributeName: ttl
  Enabled: true
```

---

## S3 Key Structure

### Existing Timelapse Prefix (unchanged)
```
<tenant_id>/<site_id>/<camera_id>/<YYYY>/<MM>/<DD>/<snapshot_ts>.jpg
```

### New Live Prefix
```
live/<tenant_id>/<site_id>/<camera_id>/<snapshot_ts>.jpg
```

The `live/` top-level prefix is the anchor for the S3 Lifecycle rule. No date partitioning is needed — these objects expire in 1 hour regardless.

---

## Ingest Handler Changes (`src/sitespy/handlers/ingest.py`)

The existing `_handle()` function is extended with two new checks inserted after the existing ingest-hours check. The overall decision tree becomes:

```
1. Validate token → resolve camera
2. Validate JPEG
3. Check ingest_hours → if outside window: return 200 skipped (unchanged)
4. [NEW] Cadence check
   - Read latest IMG# record (get_latest_img_record)
   - If exists and ingested_at < 15 min ago → set save_timelapse = False
   - If none or ≥ 15 min ago → set save_timelapse = True
   - If DynamoDB error → log, set save_timelapse = True (fail open)
5. [NEW] Live session check
   - Read SESSION# record (get_live_session)
   - If exists and expires_at > now → set save_live = True
   - If none, expired, or DynamoDB error → set save_live = False
6. If not save_timelapse and not save_live → return 200 cadence_filter skipped
7. If save_timelapse → write to timelapse S3 prefix + put IMG# record
8. If save_live → write to live/ S3 prefix + put LIVE_IMG# record
9. Build and return response
```

### Response shapes

**Both timelapse and live written (201):**
```json
{
  "key": "acme/site_01/cam_01/2025/06/10/2025-06-10T14:00:00Z.jpg",
  "timestamp": "2025-06-10T14:00:00Z",
  "camera_id": "cam_01",
  "sha256": "abc123...",
  "size_bytes": 204800,
  "live_captured": true
}
```

**Only timelapse written (201):**
```json
{
  "key": "acme/site_01/cam_01/2025/06/10/2025-06-10T14:00:00Z.jpg",
  "timestamp": "2025-06-10T14:00:00Z",
  "camera_id": "cam_01",
  "sha256": "abc123...",
  "size_bytes": 204800,
  "live_captured": false
}
```

**Only live written — cadence suppressed timelapse (200):**
```json
{
  "status": "skipped",
  "reason": "cadence_filter",
  "camera_id": "cam_01",
  "live_captured": true
}
```

**Neither written — cadence suppressed, no active session (200):**
```json
{
  "status": "skipped",
  "reason": "cadence_filter",
  "camera_id": "cam_01",
  "live_captured": false
}
```

---

## Live Session Handler (`src/sitespy/handlers/live_session.py`)

A single new Lambda handler file with three entry points dispatched to by SAM. Follows the same structure as existing handlers (Powertools Logger/Metrics, `_handle_*` inner functions, `error_response` / `json_response` helpers).

### `handler_post` — POST /v1/sites/{site_id}/cameras/{camera_id}/live-session

```
1. Extract correlation_id
2. Extract claims → resolve (role, tenant_id, site_access)
3. Resolve tenant_id (super_admin requires ?tenant_id= query param)
4. Authorise (site_access check for Users; tenant check for Tenant_Admins)
5. Verify camera exists (GetItem Camera_Record) → 404 if not
6. Check for existing session (GetItem SESSION# record)
   - If exists and expires_at > now → 409 SESSION_ALREADY_ACTIVE
   - DynamoDB error → 500 INTERNAL_ERROR
7. now = datetime.now(UTC)
   expires_at = now + timedelta(minutes=10)
   ttl = int(expires_at.timestamp()) + 3600
   session_id = str(uuid4())
8. PutItem SESSION# record with ConditionExpression attribute_not_exists(SK)
   - ConditionalCheckFailedException → 409 SESSION_ALREADY_ACTIVE
   - Other exception → 500 INTERNAL_ERROR
9. Return 201 { session_id, expires_at, camera_id }
```

### `handler_get` — GET /v1/sites/{site_id}/cameras/{camera_id}/live-session

```
1. Extract correlation_id
2. Extract claims → resolve role/tenant/access
3. Authorise
4. GetItem SESSION# record
   - DynamoDB error → 500
   - Not found or expires_at <= now → return 200 { status: "none" }
5. Query LIVE_IMG# records (ScanIndexForward=False, Limit=1)
   - DynamoDB error → log, return 200 { status, session_id, expires_at } (no latest_image)
   - No records → return 200 { status: "active", session_id, expires_at, latest_image: null }
6. Generate presigned URL for the live S3 object (300s TTL)
7. Return 200 {
     status: "active",
     session_id,
     expires_at,
     latest_image: { presigned_url, captured_at, expires_in: 300 }
   }
```

### `handler_delete` — DELETE /v1/sites/{site_id}/cameras/{camera_id}/live-session

```
1. Extract correlation_id
2. Extract claims → resolve role/tenant/access
3. Authorise
4. GetItem SESSION# record
   - DynamoDB error → 500
   - Not found or expires_at <= now → 404 NOT_FOUND
5. DeleteItem SESSION# record
   - DynamoDB error → 500
6. Return 200 { status: "deleted" }
```

---

## Data Layer Additions (`src/sitespy/data.py`)

Four new functions:

```python
def build_session_sk(site_id: str, camera_id: str) -> str:
    return f"SESSION#{site_id}#{camera_id}"

def build_live_img_sk(site_id: str, camera_id: str, snapshot_ts: str) -> str:
    return f"LIVE_IMG#{site_id}#{camera_id}#{snapshot_ts}"

def get_live_session(tenant_id: str, site_id: str, camera_id: str) -> Mapping[str, Any] | None:
    """GetItem SESSION# record. Returns None if not found."""

def put_live_session(
    tenant_id: str, site_id: str, camera_id: str,
    session_id: str, expires_at: str, ttl: int,
    created_by: str, created_at: str,
) -> None:
    """PutItem with ConditionExpression attribute_not_exists(SK).
    Raises botocore ConditionalCheckFailedException on duplicate."""

def delete_live_session(tenant_id: str, site_id: str, camera_id: str) -> None:
    """DeleteItem SESSION# record."""

def get_latest_live_img_record(
    tenant_id: str, site_id: str, camera_id: str,
) -> Mapping[str, Any] | None:
    """Query LIVE_IMG# SK prefix, ScanIndexForward=False, Limit=1."""

def put_live_img_record(
    tenant_id: str, site_id: str, camera_id: str,
    snapshot_ts: str, s3_key: str,
    sha256_hex: str, size_bytes: int, ttl: int,
) -> None:
    """PutItem LIVE_IMG# record."""
```

---

## Storage Layer Additions (`src/sitespy/storage.py`)

One new key builder and one new write function:

```python
def build_live_snapshot_key(
    tenant_id: str, site_id: str, camera_id: str, snapshot_ts: str,
) -> str:
    """Returns: live/<tenant_id>/<site_id>/<camera_id>/<snapshot_ts>.jpg"""

def put_live_snapshot(
    key: str, body: bytes, sha256_hex: str, snapshot_ts: str, tenant_id: str,
) -> None:
    """Write JPEG to S3 live/ prefix. No retention tag — cleaned by Lifecycle."""
```

The existing `generate_presigned_url` is reused unchanged for both timelapse and live images.

---

## Infrastructure Changes (`template.yaml`)

### 1. DynamoDB TTL

Add `TimeToLiveSpecification` to the existing `DataTable` resource:

```yaml
TimeToLiveSpecification:
  AttributeName: ttl
  Enabled: true
```

### 2. S3 Lifecycle Rule

Add a new rule to the existing `LifecycleConfiguration` on `SnapshotsBucket`:

```yaml
- Id: ExpireLiveSnapshotsAfter1Hour
  Status: Enabled
  Filter:
    Prefix: live/
  Expiration:
    Days: 1
```

> Note: S3 Lifecycle minimum granularity is 1 day. To achieve ~1 hour expiry, use `AbortIncompleteMultipartUpload` with `DaysAfterInitiation: 1` is not applicable here. The practical approach: set `Days: 1` and accept that live objects expire within 24 hours rather than exactly 1 hour. The DynamoDB TTL on LIVE_IMG# records will expire references within ~48 hours regardless. If exact 1-hour S3 expiry is required in future, an EventBridge rule + Lambda can be added, but that is out of scope for this phase.

### 3. New Lambda + API Gateway Routes

```yaml
LiveSessionFunction:
  Type: AWS::Serverless::Function
  Properties:
    FunctionName: !Sub sitespy-${Environment}-live-session
    CodeUri: src/
    Handler: sitespy.handlers.live_session.handler_post
    Description: Manages live view sessions (POST/GET/DELETE).
    Policies:
      - DynamoDBCrudPolicy:
          TableName: !Ref DataTable
      - Statement:
          - Effect: Allow
            Action:
              - s3:GetObject
            Resource: !Sub "arn:aws:s3:::${SnapshotsBucket}/*"
    Events:
      PostLiveSession:
        Type: Api
        Properties:
          RestApiId: !Ref SiteSpyApi
          Path: /v1/sites/{site_id}/cameras/{camera_id}/live-session
          Method: POST
      GetLiveSession:
        Type: Api
        Properties:
          RestApiId: !Ref SiteSpyApi
          Path: /v1/sites/{site_id}/cameras/{camera_id}/live-session
          Method: GET
      DeleteLiveSession:
        Type: Api
        Properties:
          RestApiId: !Ref SiteSpyApi
          Path: /v1/sites/{site_id}/cameras/{camera_id}/live-session
          Method: DELETE
```

The IngestFunction also needs `s3:PutObject` on the `live/` prefix, but since the existing policy grants `s3:PutObject` on `arn:aws:s3:::${SnapshotsBucket}/*`, no IAM change is needed — the live/ prefix is already covered.

---

## Auth Pattern

All three session endpoints use Cognito authorisation (the API Gateway default authoriser). The session handler reuses the same `_extract_claims` / `_resolve_caller` / `_check_access` pattern already present in `snapshots.py` and `sites.py`. No new auth infrastructure is required.

The access check for live sessions follows the same rules as snapshot access:
- **User**: site_id must be in `custom:site_access`
- **Tenant_Admin**: any site in their `custom:tenant_id` tenant
- **Super_Admin**: any site, must pass `?tenant_id=` query param

---

## Error Codes

Two new error keys beyond the existing set:

| Error Key | HTTP | Meaning |
|---|---|---|
| `SESSION_ALREADY_ACTIVE` | 409 | A live session already exists for this camera |
| All existing keys | — | Same as rest of API (see api_handover.md) |

---

## Ingest Performance Impact

Each 1-minute push to `/v1/ingest/{token}` now incurs up to two additional DynamoDB reads:
1. `GetItem` for the latest `IMG#` record (cadence check)
2. `GetItem` for the `SESSION#` record (live session check)

Both are single-item `GetItem` calls on the main table's primary key — approximately 0.3–1ms each at p99. The ingest Lambda already has a 30s timeout and 1024MB memory. These reads are well within budget. Both fail open on error, so they cannot cause ingest failures.

---

## Sequence Diagrams

### Normal ingest — no active session, cadence ok

```
Camera → POST /v1/ingest/{token}
  IngestFunction:
    GetItem CAMERA (existing)
    GetItem IMG#latest → none or ≥15 min ago → save_timelapse = True
    GetItem SESSION# → none → save_live = False
    PutObject S3 timelapse
    PutItem IMG#
    → 201 { key, timestamp, sha256, size_bytes, live_captured: false }
```

### Normal ingest — no active session, cadence suppressed

```
Camera → POST /v1/ingest/{token}
  IngestFunction:
    GetItem CAMERA
    GetItem IMG#latest → ingested_at < 15 min ago → save_timelapse = False
    GetItem SESSION# → none → save_live = False
    → 200 { status: skipped, reason: cadence_filter, live_captured: false }
```

### Ingest during active live session, cadence suppressed

```
Camera → POST /v1/ingest/{token}
  IngestFunction:
    GetItem CAMERA
    GetItem IMG#latest → < 15 min → save_timelapse = False
    GetItem SESSION# → active → save_live = True
    PutObject S3 live/
    PutItem LIVE_IMG#
    → 200 { status: skipped, reason: cadence_filter, live_captured: true }
```

### Frontend polls GET live-session

```
Frontend → GET /v1/sites/{site_id}/cameras/{camera_id}/live-session
  LiveSessionFunction:
    Authorise caller
    GetItem SESSION# → active
    Query LIVE_IMG# (latest 1) → found
    GeneratePresignedUrl (300s)
    → 200 {
        status: active,
        session_id: "...",
        expires_at: "2025-06-10T14:10:00Z",
        latest_image: {
          presigned_url: "https://s3...",
          captured_at: "2025-06-10T14:04:00Z",
          expires_in: 300
        }
      }
```

---

## Files Changed / Created

| File | Change |
|---|---|
| `src/sitespy/handlers/ingest.py` | Add cadence check + live session check to `_handle()` |
| `src/sitespy/handlers/live_session.py` | **New** — three handler entry points |
| `src/sitespy/data.py` | Add 6 new functions for SESSION# and LIVE_IMG# records |
| `src/sitespy/storage.py` | Add `build_live_snapshot_key` and `put_live_snapshot` |
| `template.yaml` | Add TTL spec, S3 Lifecycle rule, LiveSessionFunction |
| `docs/live_view_handover.md` | **New** — frontend handover document |
