# Implementation Plan: Ingest Pipeline (Phase 0, Milestone 1)

## Overview

Build `POST /v1/ingest` end-to-end: per-camera HTTP Basic Auth against credentials on the camera's DynamoDB row, SHA-256 integrity, canonical S3 key, `IMG#` record, idempotent overwrite, canonical error envelope, one structured log line per request.

Language: Python 3.12 on AWS Lambda arm64. Test stack: `pytest`, `hypothesis==6.122.3`, `moto`, `pytest-mock`, `aws-lambda-powertools` (already the project default). New runtime dep: `bcrypt==4.2.1`.

Approach:

- **Exploration-first PBT.** The first test-and-implement cycle is the P2 canonical-key round-trip: write the property, watch it fail, then shape `storage.py` against that failure.
- **Properties live with their component.** Pure properties (P2) go against the pure module; handler-level properties (P1, P3–P8) go after the handler is assembled.
- **Tests are REQUIRED by default.** Every property and every supporting test is `- [ ]`, not `- [ ]*`. The optional marker is reserved for genuinely optional work; nothing below is genuinely optional.

## Tasks

- [x] 1. Scaffold `src/sitespy/` package and runtime deps
  - [x] 1.1 Create `src/sitespy/` and `src/sitespy/handlers/` directories with empty `__init__.py` files
  - [x] 1.2 Pin `bcrypt==4.2.1` in `src/requirements.txt` (alongside the existing `aws-lambda-powertools`, `boto3`, `pydantic` entries — add those too if the file is empty)
  - [x] 1.3 Create `src/sitespy/errors.py` with `ApiError` plus `BadRequest` (400, `BAD_REQUEST`), `Unauthorized` (401, `UNAUTHORIZED`), `InternalError` (500, `INTERNAL_ERROR`)
  - [x] 1.4 Create `src/sitespy/http.py` with `json_response(status, body, correlation_id)`, `error_response(exc, correlation_id)`, `unhandled_error_response(correlation_id)` — all attach `X-Correlation-Id` and `Content-Type: application/json`
    - _Requirements: 8.1, 8.3_

- [x] 2. Scaffold test harness
  - [x] 2.1 Create `tests/requirements-dev.txt` pinning `hypothesis==6.122.3`, `moto[s3,dynamodb]`, `pytest`, `pytest-mock`, `aws-lambda-powertools[tracer]`
  - [x] 2.2 Create `tests/conftest.py` with session-scoped `aws_env` fixture (sets `SNAPSHOTS_BUCKET`, `DATA_TABLE`, `AWS_REGION=eu-west-2`, `ENVIRONMENT=test`, `LOG_LEVEL=INFO` before `get_settings` runs) plus `moto_s3` and `moto_dynamodb` fixtures that stand up the bucket (versioning on) and table (PK/SK + GSI1) matching `template.yaml`
  - [x] 2.3 Add `camera_row_factory(tenant_id, site_id, camera_id, *, username, password, cost=12, include_hash=True, include_username=True)` — writes the camera item with a freshly `bcrypt.hashpw` hash at the requested cost; returns `(username, plaintext_password)`
  - [x] 2.4 Add `tenant_row_factory(tenant_id, *, retention_years=None)` — writes the tenant row, omits the attribute when `retention_years is None`
  - [x] 2.5 Add `ingest_event(...)` REST event builder and `jpeg_body(size=1024)` helper returning `b"\xff\xd8\xff\xe0" + padding`

- [x] 3. Strip `CAMERA_SECRETS_PREFIX` and Secrets Manager from infra and config
  - [x] 3.1 Remove the `CAMERA_SECRETS_PREFIX: !Sub sitespy/${Environment}/cameras` line from `template.yaml` under `Globals.Function.Environment.Variables`
  - [x] 3.2 Remove the entire `- Statement:` block granting `secretsmanager:GetSecretValue` from `IngestFunction.Properties.Policies` in `template.yaml` — keep `S3WritePolicy` and `DynamoDBCrudPolicy`
  - [x] 3.3 Create `src/sitespy/config.py` with an `@lru_cache` `get_settings()` returning a frozen dataclass/pydantic model exposing `snapshots_bucket`, `data_table`, `aws_region`, `environment`, `log_level` — no `camera_secrets_prefix` field

- [-] 4. Exploration property test — P2 canonical key bijection (red first)
  - [ ] 4.1 Write `tests/sitespy/test_storage.py` containing a `@given` property over `(tenant_id, site_id, camera_id)` matching `^[a-z0-9_]{1,64}$` and `snapshot_ts` matching `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`, asserting `parse_snapshot_key(build_snapshot_key(t, s, c, ts)) == (t, s, c, ts)` at `@settings(max_examples=200)`
  - [x] 4.2 Run the property test — confirm it fails (module / function does not yet exist) and record the failing example as the driver for the `storage.py` shape
    - **Property 2: Canonical Key Bijection**
    - **Validates: Requirement 5.2**

- [x] 5. Implement canonical S3 key functions in `storage.py`
  - [x] 5.1 Implement `build_snapshot_key(tenant_id, site_id, camera_id, snapshot_ts) -> str` producing `<t>/<s>/<c>/<YYYY>/<MM>/<DD>/<ts>.jpg`, with `<YYYY>/<MM>/<DD>` parsed out of `snapshot_ts` itself (single source of date components)
  - [x] 5.2 Implement `parse_snapshot_key(key) -> tuple[str, str, str, str]` as the inverse
  - [x] 5.3 Rerun the P2 property test — assert green at 200 iterations
    - **Property 2: Canonical Key Bijection**
    - **Validates: Requirement 5.2**

- [x] 6. Implement `data.py` key builders
  - [x] 6.1 Implement `build_tenant_pk(tenant_id)`, `build_tenant_sk(tenant_id)`, `build_camera_sk(site_id, camera_id)`, `build_img_sk(site_id, camera_id, snapshot_ts)` — pure functions, no AWS imports
  - [x] 6.2 Write unit tests in `tests/sitespy/test_data.py` asserting the exact string output of each builder for concrete inputs
    - _Requirements: 2.3, 6.1_

- [x] 7. Implement `camera_auth.parse_basic_auth`
  - [x] 7.1 Implement `parse_basic_auth(event) -> tuple[str, str]` in a rewritten `src/sitespy/camera_auth.py` (no Secrets Manager, no cached boto3 client) — raises `Unauthorized` on missing header, non-`Basic` scheme, non-base64, zero or multiple colons
  - [x] 7.2 Write unit tests covering the full malformed-input partition (header missing, wrong scheme, bad base64, empty decoded string, zero colons, two colons) plus a happy-path parse
    - _Requirements: 2.1, 2.2_

- [~] 8. Implement `camera_auth.verify` with bcrypt cost guard
  - [ ] 8.1 Implement `verify(username, password, camera_item) -> None` — raises `Unauthorized("Authentication failed.")` on every failure mode (missing `ingest_username`, missing `ingest_password_hash`, username mismatch via `hmac.compare_digest`, stored hash cost `< 12` parsed from the `$2b$NN$` prefix, `bcrypt.checkpw` false); cost check happens **before** `bcrypt.checkpw`
  - [x] 8.2 Write unit tests for every failure mode plus a success case — assert identical message string on every failure (Requirement 2.10)
    - _Requirements: 2.4, 2.5, 2.7, 2.8, 2.9, 2.10_
  - [x] 8.3 Write a unit test spying on `hmac.compare_digest` via `pytest-mock` — assert called once with `bytes`-typed arguments
    - _Requirements: 2.6_

- [x] 9. Implement `data.py` DynamoDB operations
  - [x] 9.1 Add a module-level `@lru_cache` `_dynamodb_client()` constructed with `botocore.config.Config(retries={"mode": "standard", "max_attempts": 3})` and `region_name=get_settings().aws_region`
  - [x] 9.2 Implement `get_camera(tenant_id, site_id, camera_id)`, `get_retention_years(tenant_id)` (default 5 plus `logger.warning` on fallback), `put_img_record(...)` — `PutItem` with no `ConditionExpression`
  - [x] 9.3 Write unit tests with `@mock_aws` for each: item present, item absent, attribute absent (retention default), put + read-back
    - _Requirements: 2.3, 5.6, 6.1, 6.2, 7.2_

- [x] 10. Implement `storage.put_snapshot`
  - [x] 10.1 Add a module-level `@lru_cache` `_s3_client()` with the same `max_attempts=3` standard-mode config
  - [x] 10.2 Implement `put_snapshot(key, body, sha256_hex, snapshot_ts, tenant_id, retention_years)` — `put_object` with `ContentType="image/jpeg"`, `Metadata={"sha256": ..., "ingested-at": ...}`, `Tagging=f"tenant_id={tenant_id}&retention_years={retention_years}"`
  - [x] 10.3 Write unit tests with `@mock_aws` asserting exact `x-amz-meta-sha256`, `x-amz-meta-ingested-at`, and `GetObjectTagging` values
    - _Requirements: 5.3, 5.4, 5.5, 5.6_

- [x] 11. Checkpoint — pure and unit layers green
  - Ensure all tests pass, ask Fin if questions arise.

- [-] 12. Implement `handlers/ingest.py`
  - [ ] 12.1 Implement `resolve_correlation_id(event)` — reuse the `X-Correlation-Id` header verbatim when it matches `^[A-Za-z0-9_-]{1,128}$`, else generate `uuid.uuid4()`
    - _Requirements: 10.1_
  - [x] 12.2 Implement the outer `handler(event, _context)` wrapper per `backend-python.md` shape — Powertools decorators, `ApiError` → `error_response` + `IngestFailure` metric + `WARNING` log, raw `Exception` → `unhandled_error_response` + `IngestFailure` metric + `logger.exception`, success → `IngestSuccess` metric + `INFO` log; exactly one structured log line per invocation with `correlation_id`, `tenant_id`, `site_id`, `camera_id`, `route`, `status_code`, `latency_ms`, `sha256` (on 201), `error` / `failure_reason` (on non-2xx)
    - _Requirements: 8.1, 8.2, 8.3, 10.2, 10.3, 10.4, 10.5_
  - [x] 12.3 Implement `_handle(event)` straight-line per the design pseudocode — validate IDs and body, `parse_basic_auth`, `data.get_camera`, `camera_auth.verify`, generate UTC `snapshot_ts` at 1 s precision, compute `sha256_hex`, `storage.build_snapshot_key`, `data.get_retention_years`, `storage.put_snapshot`, `data.put_img_record`, return 201 envelope `{key, timestamp, camera_id, sha256}`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 2.3, 3.1, 3.2, 3.3, 4.1, 4.2, 4.3, 5.1, 5.2, 5.7, 6.3, 6.4, 7.1, 7.3, 9.1, 9.2, 9.3, 9.4, 9.5_

- [x] 13. Handler-level property tests
  - [x] 13.1 P1 Integrity — write `tests/sitespy/handlers/test_ingest_happy_path.py` with `@mock_aws` + `hypothesis` generators for regex-valid IDs and JPEG bodies 3 B – 1 MiB (magic-byte prefixed); assert four-way SHA-256 equality across response / `IMG#` record / S3 metadata / `GetObject` body, plus `size_bytes`, `s3_key`, `ingested_at`, `content_type`, `tenant_id` tag, `retention_years` tag; `@settings(max_examples=100)`
    - **Property 1: Integrity — Transitive Hash Equality**
    - **Validates: Requirements 4.1, 4.2, 4.3, 5.3, 5.4, 5.5, 5.6, 6.1, 6.2, 9.1, 9.2, 9.3, 9.4, 9.5**
  - [x] 13.2 P3 Idempotency — run `_handle(event)` twice with identical bytes + frozen clock; `@given` varies the second body to prove second-write-wins; assert exactly one current S3 version at the key and exactly one `IMG#` item
    - **Property 3: Idempotency**
    - **Validates: Requirements 7.1, 7.2, 7.3**
  - [x] 13.3 P4 Auth binding — spy on the DynamoDB client via `pytest-mock`; `@given` over regex-valid `(t, s, c)` and every auth outcome (success, item missing, attrs missing, cost low, username mismatch, password mismatch); assert exactly one `get_item` call with `Key == {"PK": "TENANT#"+t, "SK": "SITE#"+s+"#CAM#"+c}` by byte comparison
    - **Property 4: Authentication Binding — Structural Invariant**
    - **Validates: Requirements 2.3, 3.2, 3.3**
  - [x] 13.4 P5 Ordering — monkey-patch `data.put_img_record` to raise `ClientError` after a successful `storage.put_snapshot`; assert 500, S3 object present, zero `IMG#` items. Second case: monkey-patch `storage.put_snapshot` to raise; assert 500 and zero `put_item` calls
    - **Property 5: Ordering — No Orphan IMG Records**
    - **Validates: Requirements 5.7, 6.3, 6.4**
  - [x] 13.5 P6 Error envelope closure — parameterise over every non-2xx cause in the design's error matrix; assert `status ∈ {400, 401, 500}`, body parses as JSON, `error ∈ {"BAD_REQUEST", "UNAUTHORIZED", "INTERNAL_ERROR"}`, `X-Correlation-Id` header present, every 401 response body is byte-identical
    - **Property 6: Error Envelope Closure**
    - **Validates: Requirements 2.10, 3.1, 8.1, 8.2, 8.3**
  - [x] 13.6 P7 Secret redaction — run every P6 scenario plus a happy path; scan every record in `caplog` and the captured EMF output as strings for the raw `Authorization` header value, the decoded username, the decoded password, and the stored `ingest_password_hash`; assert zero matches
    - **Property 7: Secret Redaction**
    - **Validates: Requirement 10.5**
  - [x] 13.7 P8 Bcrypt cost confinement — insert a cost-10 hash and submit the matching plaintext; assert 401. Parameterise over costs `[4, 8, 10, 11, 12, 13]`; assert 201 iff `cost >= 12`
    - **Property 8: Bcrypt Cost Confinement**
    - **Validates: Requirement 2.9**

- [x] 14. Supporting tests (requirements without a P)
  - [x] 14.1 Header/body validation partition — `test_ingest_validation.py` with `hypothesis` generators for missing fields, regex-invalid IDs, empty body, bad magic-bytes prefix, bodies at `10 MiB − 1`, `10 MiB`, `10 MiB + 1`
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_
  - [x] 14.2 Correlation ID reuse and generation — `test_ingest_correlation_id.py`; header returned verbatim when input matches regex, else a fresh UUID v4 in the response header
    - _Requirements: 10.1_
  - [x] 14.3 Single structured log line plus EMF metrics — `test_ingest_metrics.py`; exactly one INFO/WARNING/ERROR record per invocation with all required fields; `IngestSuccess` / `IngestFailure` name, namespace `SiteSpy`, dimensions (`tenant_id`, `site_id`, `camera_id` for success; plus `status_code` for failure; `unknown` when not extractable)
    - _Requirements: 10.2, 10.3, 10.4_
  - [x] 14.4 S3 retries — `@pytest.mark.integration` test in `test_storage.py` using LocalStack or a fault-injecting transport returning 503 twice then 200; assert eventual `put_snapshot` success after three attempts
    - _Requirements: 5.8_

- [x] 15. Checkpoint — full property and support suite green
  - Ensure all tests pass, ask Fin if questions arise.

- [x] 16. Local build validation
  - [x] 16.1 Run `ruff check src/ tests/` and `ruff format --check src/ tests/` — resolve any issues
  - [x] 16.2 Run `mypy --strict src/sitespy/` — resolve any issues
  - [x] 16.3 Run `pytest -m 'not integration'` — full unit and property suite green
  - [x] 16.4 Run `sam build` — package compiles for the arm64 Lambda runtime

- [-] 17. Deploy to dev — **requires Fin's approval before running** (per `.kiro/steering/conventions.md`)
  - [x] 17.1 Ask Fin to approve; on approval, run `sam deploy --config-env dev`; capture `ApiEndpoint` and `IngestUrl` outputs

- [ ] 18. Write and run deployed-endpoint smoke test
  - [ ] 18.1 Write `scripts/smoke_ingest.py` that POSTs a generated JPEG (magic bytes + padding) to the deployed `/v1/ingest` with `X-Tenant-ID`, `X-Site-ID`, `cameraID`, `Content-Type: image/jpeg`, and valid Basic Auth for a seeded test camera (reference `test_server/app.py` for request shape)
  - [ ] 18.2 Assert the response is 201; fetch the S3 object at the returned key and the `IMG#` item from DynamoDB; assert `sha256(local_body) == response.sha256 == IMG#.sha256 == s3.metadata["sha256"]`
    - _Requirements: 1.1, 4.3, 5.2, 6.1, 6.2, 9.1, 9.2, 9.3, 9.4, 9.5_

- [ ] 19. Verify test-tenant cleanup
  - [ ] 19.1 Delete the seeded test camera item, tenant item, S3 object (and its versioned predecessor), and `IMG#` item used for the smoke
  - [ ] 19.2 Rerun the smoke; assert 401 on ingest (no camera row → no existence oracle) and confirm no orphan items remain under `PK = TENANT#<test_tenant>`

## Notes

**Follow-up items (tracked outside this spec):**

- Reconciliation pass on `requirements/api_contract.md`, `requirements/multi-tenant-auth.md`, and `requirements/software_logic.md` to remove Secrets Manager references from the ingest credential path. Out of scope for this spec.
- Cleanup of the 404 / 409 rows in the `requirements/api_contract.md` ingest error table. Same reconciliation pass.

**Conventions:**

- Tests are REQUIRED (`- [ ]`) by default. Nothing in this plan is marked `- [ ]*`.
- Every property task references its property number and the requirement clauses it validates.
- Every supporting test task references the requirement clauses it covers.
- Checkpoints (tasks 11 and 15) exist to catch regressions early without requiring a full-suite rerun at every step.
- `sam deploy` (task 17) is the only task that requires explicit Fin approval before execution.
