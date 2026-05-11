# Requirements Document

## Introduction

This spec covers **Milestone 1 of Phase 0**: the ingest pipeline from "Axis camera POSTs a JPEG" to "image lands in S3 with an integrity record in DynamoDB."

Scope:

- `POST /v1/ingest` endpoint, public, per-camera HTTP Basic Auth (credentials on the camera's DynamoDB row, bcrypt-hashed).
- Single-read credential lookup and existence check against the camera item in the DynamoDB Site Mapping table. Defense-in-depth binding is structural — credentials physically live on the camera's own row, so cross-camera misuse is impossible by construction.
- SHA-256 hashing of every uploaded JPEG on arrival (Phase 0 foundation for Dispute Mode — see `requirements/roadmap.md` backlog item B.1).
- S3 write with canonical key, integrity metadata, and retention tagging.
- DynamoDB `IMG#` record with hash, size, and ingest timestamp.
- Idempotent overwrite on duplicate keys.
- Canonical error envelope with stable status codes (400 / 401 / 500). The ingest path does not return 404 — unknown-camera POSTs return 401 so an unauthenticated caller cannot probe for the existence of a tenant, site, or camera.
- Structured logging with correlation IDs and success/failure metrics.

Out of scope (covered in later milestones):

- Cognito, dashboard auth, and role resolution.
- The flag pipeline itself and the stale-image auto-flag resolver. The `IngestSuccess` metric defined below is the hook point a later milestone will subscribe to.
- Admin / CRUD APIs (tenants, users, cameras, sites). Provisioning in Phase 0 is handled by the seed script. Credential minting (generating the random username and bcrypt-hashing the random password at registration) lives with `POST /v1/sites/{site_id}/cameras`, which is outside this spec.
- Dashboard UI, timelapse generator, and any other consumer of the stored data.
- CloudWatch alarms, dashboards, and paging thresholds — this spec emits the metrics; routing them is a separate observability milestone.

Regional context: AWS `eu-west-2` (London). Runtime: Python 3.12 on AWS Lambda via SAM. All references to the broader system follow `requirements/api_contract.md`, `requirements/multi-tenant-auth.md` §5, `requirements/software_logic.md` §8, and `requirements/roadmap.md`.

### Consistency Note — Follow-up Pass Required

`requirements/api_contract.md`, `requirements/multi-tenant-auth.md` §5, and `requirements/software_logic.md` §1 still describe AWS Secrets Manager as the ingest credential store system-wide. Those documents require a follow-up reconciliation pass once this spec is approved: remove Secrets Manager from the ingest credential path, replace with the DynamoDB camera item described below, and clean up the `409 CONFLICT` and `404 NOT_FOUND` rows in the `api_contract.md` error table for the ingest endpoint. That cleanup is intentionally out of scope for this spec.

### Resolved Ambiguities

1. **Snapshot timestamp origin.** Server-generated at ingest time, UTC ISO8601, 1-second precision (`YYYY-MM-DDTHH:mm:ssZ`). The camera does not supply a timestamp. Rationale: eliminates a clock-skew / clock-drift attack surface, matches the hourly cadence, and avoids adding a VAPIX header the Axis config does not currently set.
2. **Status code on idempotent duplicate.** `201 Created` on every successful write, including overwrites. Rationale: keeps the camera's retry logic trivial, aligns with S3 versioning semantics (each `PutObject` produces a new version and the "current object" is deterministic). The stale `409 CONFLICT — Duplicate ingest` row in the `requirements/api_contract.md` error table will be cleaned up separately; this spec does not produce a 409 on the ingest path.
3. **Credential storage mechanism.** Per-camera ingest credentials live as two attributes on the existing DynamoDB camera item (`PK = TENANT#<tenant_id>`, `SK = SITE#<site_id>#CAM#<camera_id>`): `ingest_username` (plaintext, 32-char random string prefixed `sitespy_cam_`) and `ingest_password_hash` (bcrypt hash of a 48-char random password, cost factor 12). AWS Secrets Manager is not used by this milestone.
   - **Rationale:** cost — Secrets Manager is $0.40/secret/month, which at 10k cameras is ~£3,150/month for data that changes only on rotation. One fewer AWS dependency, one fewer IAM surface, one DynamoDB read instead of two. Defense-in-depth becomes structural: a credential is physically on the camera's own row, so it cannot, by construction, authorize an upload targeted at any other camera.
   - **Trade-off:** the plaintext password is not retrievable after mint. This was already true in the prior design — rotation means minting a fresh username/password pair, not revealing the old one. No regression.

## Glossary

- **Ingest_Service** — the Lambda function and its handler that serves `POST /v1/ingest`.
- **Axis_Camera** — the Axis P1455-LE edge device that originates an ingest POST. Not owned by this spec; referenced as the client of the contract.
- **Snapshot_Bucket** — the versioned S3 bucket backing snapshot storage. Defined in `sitespy/template.yaml` as `SnapshotsBucket`.
- **Site_Mapping_Table** — the single DynamoDB table holding tenant, site, camera, and image metadata. Defined in `sitespy/template.yaml` as `DataTable`. Schema in `requirements/multi-tenant-auth.md` §3 and `requirements/api_contract.md`.
- **Credential_Store** — the camera item in the Site_Mapping_Table itself (keyed `PK = TENANT#<tenant_id>`, `SK = SITE#<site_id>#CAM#<camera_id>`), carrying the attributes `ingest_username` and `ingest_password_hash`. No separate service is involved — credentials are collocated with the camera row they authorize.
- **Ingest_Username** — the `ingest_username` attribute on the camera item. A 32-character random string prefixed with `sitespy_cam_`, stored as plaintext. Minted at camera registration (out of scope here).
- **Ingest_Password_Hash** — the `ingest_password_hash` attribute on the camera item. A bcrypt hash (cost factor 12) of a 48-character random password minted at camera registration. The plaintext password is shown exactly once at mint and never stored anywhere in SiteSpy.
- **Bcrypt_Cost_Default** — the project default bcrypt cost parameter, value `12`. All stored `ingest_password_hash` values must be produced at or above this cost.
- **Tenant_Id / Site_Id / Camera_Id** — lowercase slug identifiers matching the regex `^[a-z0-9_]{1,64}$`, assigned at provisioning time in a separate milestone.
- **Snapshot_Timestamp** — the server-generated UTC ISO8601 timestamp at 1-second precision, format `YYYY-MM-DDTHH:mm:ssZ`.
- **Canonical_Key** — the S3 object key `<tenant_id>/<site_id>/<camera_id>/<YYYY>/<MM>/<DD>/<Snapshot_Timestamp>.jpg`, where `<YYYY>`, `<MM>`, and `<DD>` are the UTC date components of `Snapshot_Timestamp`.
- **IMG_Record** — the DynamoDB item keyed `PK = TENANT#<tenant_id>`, `SK = IMG#<site_id>#<camera_id>#<Snapshot_Timestamp>`.
- **Correlation_Id** — a UUID v4 string that tags every log record and metric emitted while serving a single ingest request.

## Requirements

### Requirement 1: Ingest Endpoint Contract

**User Story:** As an Axis camera on a construction site, I want to POST a JPEG snapshot to a stable HTTPS endpoint, so that every hourly snapshot reliably reaches cloud storage.

#### Acceptance Criteria

1. THE Ingest_Service SHALL accept HTTP POST requests at the path `/v1/ingest`.
2. WHEN a POST request arrives at `/v1/ingest`, THE Ingest_Service SHALL require an `Authorization` header whose scheme is `Basic`, an `X-Tenant-ID` header, an `X-Site-ID` header, a `cameraID` query parameter, and a `Content-Type: image/jpeg` header.
3. IF any of the required headers or the `cameraID` query parameter is missing or empty, THEN THE Ingest_Service SHALL return HTTP 400 with error key `BAD_REQUEST`.
4. IF the `X-Tenant-ID`, `X-Site-ID`, or `cameraID` value does not match the regex `^[a-z0-9_]{1,64}$`, THEN THE Ingest_Service SHALL return HTTP 400 with error key `BAD_REQUEST`.
5. IF the request body is empty, THEN THE Ingest_Service SHALL return HTTP 400 with error key `BAD_REQUEST`.
6. IF the request body does not begin with the JPEG magic bytes `FF D8 FF`, THEN THE Ingest_Service SHALL return HTTP 400 with error key `BAD_REQUEST`.
7. IF the request body exceeds 10,485,760 bytes (10 MiB), THEN THE Ingest_Service SHALL return HTTP 400 with error key `BAD_REQUEST`.

### Requirement 2: Per-Camera HTTP Basic Authentication

**User Story:** As the security owner of SiteSpy, I want every ingest request to authenticate against credentials stored on the camera's own DynamoDB row, so that a compromised credential cannot, by construction, authorize an upload to any other camera's path.

#### Acceptance Criteria

1. WHEN an ingest request has passed the Requirement 1 checks, THE Ingest_Service SHALL parse the `Authorization: Basic <base64>` header and base64-decode its value into a `username:password` pair.
2. IF the `Authorization` header is missing, uses a scheme other than `Basic`, is not base64-decodable, or the decoded value does not contain exactly one `:` separator, THEN THE Ingest_Service SHALL return HTTP 401 with error key `UNAUTHORIZED`.
3. WHEN the username and password have been parsed, THE Ingest_Service SHALL read the camera item from the Site_Mapping_Table using a single DynamoDB `GetItem` call keyed `PK = TENANT#<tenant_id>`, `SK = SITE#<site_id>#CAM#<camera_id>`, where `<tenant_id>`, `<site_id>`, and `<camera_id>` are the values of the request's `X-Tenant-ID` header, `X-Site-ID` header, and `cameraID` query parameter.
4. IF the `GetItem` call returns no item, THEN THE Ingest_Service SHALL return HTTP 401 with error key `UNAUTHORIZED`.
5. IF the returned camera item is missing the `ingest_username` attribute or the `ingest_password_hash` attribute, THEN THE Ingest_Service SHALL return HTTP 401 with error key `UNAUTHORIZED`.
6. WHEN the camera item has been fetched, THE Ingest_Service SHALL compare the submitted username to the item's `ingest_username` attribute using a constant-time comparison (`hmac.compare_digest` or equivalent).
7. WHEN the username comparison has been performed, THE Ingest_Service SHALL verify the submitted password against the item's `ingest_password_hash` attribute using `bcrypt.checkpw(submitted_password_bytes, ingest_password_hash_bytes)`.
8. IF the username comparison fails or the `bcrypt.checkpw` verification returns false, THEN THE Ingest_Service SHALL return HTTP 401 with error key `UNAUTHORIZED`.
9. IF the cost parameter encoded in the stored `ingest_password_hash` is less than Bcrypt_Cost_Default (12), THEN THE Ingest_Service SHALL return HTTP 401 with error key `UNAUTHORIZED` and SHALL NOT treat the hash as valid regardless of the `bcrypt.checkpw` outcome.
10. THE Ingest_Service SHALL return the same HTTP 401 error key (`UNAUTHORIZED`) and the same human-readable message for every authentication failure mode (missing camera item, missing credential attribute, bad username, bad password, sub-default cost factor), so that a client cannot distinguish between them.

### Requirement 3: Structural Binding and No Existence Oracle on Ingest

**User Story:** As the security owner of SiteSpy, I want unknown-camera POSTs to return 401 rather than 404, and I want camera existence validation to be inherent to authentication, so that an unauthenticated caller cannot probe for the existence of a tenant, site, or camera, and the ingest path does not make a second DynamoDB read.

#### Acceptance Criteria

1. THE Ingest_Service SHALL NOT return HTTP 404 on the `POST /v1/ingest` path for any reason.
2. THE Ingest_Service SHALL NOT perform any DynamoDB read of the Site_Mapping_Table beyond the single `GetItem` described in Requirement 2.3, on the path from request receipt to the start of the S3 write in Requirement 5.
3. THE `ingest_username` and `ingest_password_hash` attributes consulted by the Ingest_Service SHALL reside on the camera item identified by the request's `tenant_id`, `site_id`, and `camera_id` — so that, by the construction of the storage layout, a credential cannot authorize an upload targeted at any other camera.

### Requirement 4: SHA-256 Image Integrity Computation

**User Story:** As the product owner, I want every ingested JPEG hashed on arrival, with the hash stored independently in S3 object metadata and in a DynamoDB record, so that Phase 0 captures the tamper-evidence foundation that Dispute Mode (`requirements/roadmap.md` B.1) will consume without a future archive rehash.

#### Acceptance Criteria

1. WHEN authentication has succeeded, THE Ingest_Service SHALL compute the SHA-256 digest of the raw request body bytes, producing a 64-character lowercase hexadecimal string.
2. THE Ingest_Service SHALL compute the SHA-256 digest from the exact byte sequence that is subsequently written as the S3 object body.
3. THE Ingest_Service SHALL include the computed SHA-256 digest in the HTTP 201 response body under the field name `sha256`.

### Requirement 5: S3 Write with Canonical Key, Metadata, and Retention Tag

**User Story:** As the platform operator, I want every JPEG stored under a deterministic canonical key carrying its integrity hash, ingest timestamp, and retention tag, so that storage structure, hash verification, and per-tenant lifecycle policies all work without per-object bookkeeping.

#### Acceptance Criteria

1. WHEN the SHA-256 digest has been computed, THE Ingest_Service SHALL generate the Snapshot_Timestamp by capturing the current UTC wall-clock time and formatting it as `YYYY-MM-DDTHH:mm:ssZ` at 1-second precision.
2. THE Ingest_Service SHALL write the JPEG bytes to the Snapshot_Bucket under the Canonical_Key `<tenant_id>/<site_id>/<camera_id>/<YYYY>/<MM>/<DD>/<Snapshot_Timestamp>.jpg`, where `<YYYY>`, `<MM>`, and `<DD>` are the UTC date components of the Snapshot_Timestamp.
3. THE Ingest_Service SHALL set the S3 object's `Content-Type` to `image/jpeg`.
4. THE Ingest_Service SHALL set the S3 object user metadata `x-amz-meta-sha256` to the lowercase hexadecimal SHA-256 digest computed in Requirement 4.
5. THE Ingest_Service SHALL set the S3 object user metadata `x-amz-meta-ingested-at` to the Snapshot_Timestamp.
6. THE Ingest_Service SHALL set the S3 object tags `tenant_id=<tenant_id>` and `retention_years=<retention_years>`, where `<retention_years>` is read from the `TENANT#<tenant_id>` item's `retention_years` attribute, defaulting to `5` when the attribute is absent.
7. IF the S3 `PutObject` call returns a non-retryable error, THEN THE Ingest_Service SHALL return HTTP 500 with error key `INTERNAL_ERROR`, and THE Ingest_Service SHALL skip the IMG_Record write defined in Requirement 6.
8. WHEN the S3 `PutObject` call returns a retryable error (transient 5xx, throttling, or connection failure), THE Ingest_Service SHALL retry the call up to 2 additional times with exponential backoff before treating the error as non-retryable.

### Requirement 6: DynamoDB IMG_Record Write (Ordered After S3)

**User Story:** As a Phase 0 foundation for Dispute Mode, I want every successful S3 write followed by a DynamoDB record capturing the object's integrity fields, so that backlog item B.1 can build its Merkle tree from DynamoDB alone without listing S3.

#### Acceptance Criteria

1. WHEN the S3 write has returned success, THE Ingest_Service SHALL write an IMG_Record to the Site_Mapping_Table with `PK = TENANT#<tenant_id>` and `SK = IMG#<site_id>#<camera_id>#<Snapshot_Timestamp>`.
2. THE IMG_Record SHALL include attributes `s3_key` (equal to the Canonical_Key), `sha256` (equal to the SHA-256 digest), `size_bytes` (equal to the byte length of the request body), `ingested_at` (equal to the Snapshot_Timestamp), and `content_type` (equal to `image/jpeg`).
3. THE Ingest_Service SHALL defer the IMG_Record write until the S3 write has returned success, so that the Site_Mapping_Table never contains an IMG_Record whose referenced S3 object has never existed.
4. IF the IMG_Record write fails, THEN THE Ingest_Service SHALL return HTTP 500 with error key `INTERNAL_ERROR`. The S3 object written in Requirement 5 remains in the Snapshot_Bucket and will be reconciled by a subsequent successful ingest at the same Canonical_Key.

### Requirement 7: Idempotent Overwrite on Duplicate Key

**User Story:** As the ingest pipeline, I want duplicate ingests for the same tenant / site / camera / Snapshot_Timestamp to produce an atomic overwrite rather than a duplicated record, so that retries and clock-tick collisions never yield stale or duplicated metadata.

#### Acceptance Criteria

1. WHEN an ingest request produces a Canonical_Key that already identifies a current-version object in the Snapshot_Bucket, THE Ingest_Service SHALL overwrite the current S3 object at that key with the new bytes and new metadata, producing a new current version (the prior version is preserved as a non-current version by the bucket's versioning configuration).
2. WHEN an ingest request produces an IMG_Record primary key (`TENANT#<tenant_id>` / `IMG#<site_id>#<camera_id>#<Snapshot_Timestamp>`) that already exists in the Site_Mapping_Table, THE Ingest_Service SHALL unconditionally replace the existing item's attributes with the attributes of the new object (`PutItem` without a `ConditionExpression`).
3. THE Ingest_Service SHALL return HTTP 201 on every successful write, whether the write was a first-time create or an overwrite of an existing Canonical_Key.

### Requirement 8: Canonical Error Envelope

**User Story:** As a consumer of the ingest API (the Axis camera, ops tooling, or future diagnostics), I want every non-2xx response to carry a predictable JSON envelope with a stable error key, so that I can reason about failures without parsing human-readable prose.

#### Acceptance Criteria

1. WHEN the Ingest_Service returns a status code in the range 400–599, THE Ingest_Service SHALL return a response body of shape `{"error": "<ERROR_KEY>", "message": "<human_readable>"}` with `Content-Type: application/json`.
2. THE Ingest_Service SHALL use error key `BAD_REQUEST` for HTTP 400, `UNAUTHORIZED` for HTTP 401, and `INTERNAL_ERROR` for HTTP 500. THE Ingest_Service SHALL NOT emit any other status code on the `POST /v1/ingest` path.
3. THE Ingest_Service SHALL include an `X-Correlation-Id` response header on every response, for both 2xx and non-2xx status codes.

### Requirement 9: Successful Response Shape

**User Story:** As the Axis camera (and as future diagnostics tooling), I want a small, well-defined JSON body on every successful ingest, so that a single POST-response pair is enough to correlate an upload with its stored object.

#### Acceptance Criteria

1. WHEN an ingest request succeeds, THE Ingest_Service SHALL return HTTP 201 with a JSON response body containing exactly the fields `key`, `timestamp`, `camera_id`, and `sha256`.
2. THE `key` field SHALL equal the Canonical_Key written to the Snapshot_Bucket.
3. THE `timestamp` field SHALL equal the Snapshot_Timestamp.
4. THE `camera_id` field SHALL equal the `cameraID` query parameter from the request, normalized to snake_case (`camera_id` in the response body).
5. THE `sha256` field SHALL equal the lowercase hexadecimal SHA-256 digest computed in Requirement 4.

### Requirement 10: Structured Logging, Correlation, and Metrics

**User Story:** As the platform operator, I want every ingest request to emit a structured log line plus success/failure metrics tagged with tenant, site, and camera, so that I can observe the pipeline without ad-hoc queries and so that the future flag pipeline has a well-defined hook point.

#### Acceptance Criteria

1. WHEN an ingest request is received, THE Ingest_Service SHALL establish a Correlation_Id: if the request carries an `X-Correlation-Id` header whose value matches the regex `^[A-Za-z0-9_-]{1,128}$`, THE Ingest_Service SHALL reuse that value; otherwise THE Ingest_Service SHALL generate a fresh UUID v4 value.
2. THE Ingest_Service SHALL emit exactly one structured JSON log record per request at INFO level containing at minimum the fields `correlation_id`, `tenant_id`, `site_id`, `camera_id`, `route` (equal to `POST /v1/ingest`), `status_code`, `latency_ms`, and `sha256` (the last present only when the response is HTTP 201).
3. WHEN an ingest request produces HTTP 201, THE Ingest_Service SHALL emit a CloudWatch custom metric named `IngestSuccess` with value `1`, namespace `SiteSpy`, and dimensions `tenant_id`, `site_id`, `camera_id`.
4. WHEN an ingest request produces any non-2xx response, THE Ingest_Service SHALL emit a CloudWatch custom metric named `IngestFailure` with value `1`, namespace `SiteSpy`, and dimensions `tenant_id`, `site_id`, `camera_id`, and `status_code`. The dimension value `unknown` SHALL be used for any tenant_id / site_id / camera_id value that could not be extracted from the request.
5. THE Ingest_Service SHALL redact secret material from all log records: the raw `Authorization` header value, the decoded username, the decoded password, and the stored `ingest_password_hash` value SHALL NOT appear in any log field or log message.

## Correctness Properties (for Property-Based Testing)

These properties are derived from the acceptance criteria above. They are called out explicitly because the team uses PBT and these are the invariants the ingest pipeline must uphold.

### P1: Integrity — Transitive Hash Equality (Invariant)

For any successful ingest of request body `B`:

```
sha256_hex(B)
  == IMG_Record.sha256
  == s3_object.metadata["sha256"]
  == sha256_hex(s3_get_object(Canonical_Key).body)
```

All four values are equal after a successful POST returns 201. Derived from Requirements 4.1, 4.2, 5.4, 6.2.

### P2: Canonical Key Bijection (Round Trip)

Let `build_key(tenant_id, site_id, camera_id, snapshot_timestamp)` be the function that produces the Canonical_Key, and `parse_key(key)` its inverse.

For any `(tenant_id, site_id, camera_id)` where each matches `^[a-z0-9_]{1,64}$` and any `snapshot_timestamp` matching the `YYYY-MM-DDTHH:mm:ssZ` grammar:

```
parse_key(build_key(t, s, c, ts)) == (t, s, c, ts)
```

`build_key` is total and injective. Derived from Requirement 5.2 and the Glossary definition of Canonical_Key. Parsers get a round-trip property per the PBT guidance in the workflow.

### P3: Idempotency (f ∘ f ≡ f)

Let `ingest(req)` be a successful ingest that produces Canonical_Key `k` and IMG_Record key `i`. If `ingest(req)` runs twice against the same Snapshot_Bucket and Site_Mapping_Table with identical request bytes and identical Snapshot_Timestamps:

- The Snapshot_Bucket contains exactly one current-version object at `k`.
- The Site_Mapping_Table contains exactly one item at `i`.
- The stored item's attributes reflect the second write's object.

Derived from Requirements 7.1, 7.2, 7.3.

### P4: Authentication Binding — Structural Invariant

For any ingest request `req` with headers `(tenant_id, site_id, camera_id)`, the Ingest_Service performs a single DynamoDB `GetItem` during authentication, and the key of that `GetItem` equals exactly:

```
PK = "TENANT#" + req.tenant_id
SK = "SITE#"   + req.site_id + "#CAM#" + req.camera_id
```

No trace of the Ingest_Service on the authentication path reads `ingest_username` or `ingest_password_hash` from any camera item other than the one addressed by the request's own `(tenant_id, site_id, camera_id)`. Because credentials physically reside on the row keyed by this triple, cross-camera credential reuse is impossible by construction.

Testable as: inspect the captured DynamoDB client calls on every auth path and assert the `Key` argument exactly matches the request's triple. Derived from Requirements 2.3 and 3.3.

### P5: Ordering — No Orphan IMG_Records (Invariant)

For every IMG_Record `r` observed in the Site_Mapping_Table, the S3 object at `r.s3_key` exists as a current or non-current version in the Snapshot_Bucket. Equivalently: at no point in any valid trace does an IMG_Record write-success precede the corresponding S3 write-success for the same Canonical_Key.

Derived from Requirement 6.3.

### P6: Error Envelope Closure (Invariant)

For every response whose status code lies in `[400, 599]`:

- The response body parses as JSON.
- The JSON object contains a non-empty string `error` field.
- The value of `error` is drawn from the closed set `{"BAD_REQUEST", "UNAUTHORIZED", "INTERNAL_ERROR"}`.
- The response status code lies in the closed set `{400, 401, 500}`.
- The response carries an `X-Correlation-Id` header.

Derived from Requirements 8.1, 8.2, 8.3 and from Requirement 3.1 (no 404 on the ingest path).

### P7: Secret Redaction (Invariant)

For any run of the Ingest_Service that produces log records `L`:

- No record in `L` contains the raw `Authorization` header value.
- No record in `L` contains the decoded Basic Auth username.
- No record in `L` contains the decoded Basic Auth password.
- No record in `L` contains the `ingest_password_hash` value read from the camera item.

Derived from Requirement 10.5.

### P8: Bcrypt Cost Confinement (Invariant)

For any ingest request whose authentication proceeds past the `GetItem` step, the bcrypt cost parameter encoded in the stored `ingest_password_hash` observed during password verification is greater than or equal to Bcrypt_Cost_Default (12). No authentication outcome of `success` is ever produced against a hash with a cost parameter below this default.

Testable as: for any `ingest_password_hash` value observed by the verification step in a run that returned HTTP 201, `bcrypt.hashpw`-style cost extraction yields a value `≥ 12`. A mutation test with a cost-10 hash on the camera row MUST produce HTTP 401, not 201.

Derived from Requirement 2.9.
