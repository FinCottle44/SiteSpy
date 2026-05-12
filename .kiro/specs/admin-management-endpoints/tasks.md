# Implementation Plan: Admin Management Endpoints

## Overview

Implement six new Lambda handlers for admin management operations (tenant creation, site registration, camera registration with credential minting, user creation, camera listing, and credential rotation), plus shared validation and credential generation modules. Each handler follows the established aws-lambda-powertools pattern with structured logging, metrics, correlation ID propagation, and the canonical `ApiError` error envelope. The SAM template is updated with new Lambda function resources and API Gateway events.

## Tasks

- [x] 1. Create shared modules (validation and credential generation)
  - [x] 1.1 Create `src/sitespy/validation.py` with field-level validators
    - Implement `validate_tenant_id`, `validate_site_id`, `validate_camera_id`, `validate_email`, `validate_latitude`, `validate_longitude`, `validate_timezone` functions
    - Define regex constants: `TENANT_ID_RE = ^[a-z0-9_]{3,32}$`, `SITE_ID_RE = ^[a-z0-9_]{1,64}$`, `CAMERA_ID_RE = ^[a-z0-9_]{1,64}$`, `EMAIL_RE = ^[^@\s]+@[^@\s]+\.[^@\s]+$`
    - Use `zoneinfo.ZoneInfo` for timezone validation
    - _Requirements: 1.4, 1.5, 1.6, 2.6, 2.7, 2.8, 2.11, 3.6, 3.7, 3.8, 4.7, 4.8, 4.9_

  - [x] 1.2 Create `src/sitespy/credentials.py` with credential generation
    - Implement `generate_credential_pair() -> tuple[str, str]` using `secrets.choice`
    - Username: 32 chars total, prefix `sitespy_cam_` + 20 random alphanumeric
    - Password: 48 random alphanumeric chars (mixed case + digits)
    - _Requirements: 3.10, 6.6_

  - [x] 1.3 Write property tests for validation module
    - **Property 2: ID format validation rejects non-conforming identifiers**
    - **Property 9: Latitude/longitude range validation**
    - **Property 10: stale_threshold_hours range validation**
    - **Validates: Requirements 1.4, 1.8, 2.6, 2.8, 3.6**
    - Create `tests/sitespy/test_validation_properties.py`
    - Use Hypothesis strategies to generate strings violating regex patterns and floats outside valid ranges

  - [x] 1.4 Write property tests for credential generation
    - **Property 4: Credential pair generation format invariant**
    - **Validates: Requirements 3.10, 6.6**
    - Create `tests/sitespy/test_credentials_properties.py`
    - Assert username is exactly 32 chars with `sitespy_cam_` prefix + 20 alphanumeric
    - Assert password is exactly 48 alphanumeric chars

- [x] 2. Add data layer write operations
  - [x] 2.1 Add write functions to `src/sitespy/data.py`
    - Implement `put_tenant(tenant_id, tenant_name, primary_contact_email, stale_threshold_hours)` with `ConditionExpression attribute_not_exists(PK)`
    - Implement `put_site(tenant_id, site_id, site_name, latitude, longitude, timezone)` with ConditionExpression
    - Implement `put_camera(tenant_id, site_id, camera_id, camera_name, camera_model)` with ConditionExpression
    - Implement `delete_camera(tenant_id, site_id, camera_id)` for rollback scenarios
    - Implement `get_tenant(tenant_id)` if not already present
    - _Requirements: 1.1, 1.9, 2.12, 2.13, 3.11, 3.12, 3.14_

- [x] 3. Implement POST /v1/tenants handler
  - [x] 3.1 Create `src/sitespy/handlers/tenants_post.py`
    - Follow existing handler pattern (powertools decorators, correlation ID, try/except ApiError)
    - Extract claims, resolve role, require `super_admin`
    - Parse JSON body, validate required fields (`tenant_id`, `tenant_name`, `primary_contact_email`)
    - Apply default `stale_threshold_hours=24` when omitted
    - Validate `stale_threshold_hours` in [1, 720] when provided
    - Call `data.put_tenant(...)`, catch `ConditionalCheckFailedException` → 409
    - Return 201 with tenant record
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 1.10, 1.11_

  - [x] 3.2 Write property tests for tenants_post handler
    - **Property 1: Role enforcement rejects unauthorized callers**
    - **Property 3: Missing required fields produce 400**
    - **Property 5: Valid creation round-trip preserves all input fields**
    - **Property 8: Invalid JSON body produces 400**
    - **Validates: Requirements 1.2, 1.3, 1.11, 7.7**
    - Create `tests/sitespy/test_tenants_post_properties.py`

  - [x] 3.3 Write unit tests for tenants_post handler
    - Test happy-path creation → 201
    - Test duplicate tenant_id → 409
    - Test missing required fields → 400
    - Test invalid tenant_id format → 400
    - Test stale_threshold_hours default (24) and out-of-range → 400
    - Test non-super_admin caller → 403
    - Create `tests/sitespy/test_tenants_post.py`
    - _Requirements: 1.1–1.11_

- [x] 4. Implement POST /v1/sites handler
  - [x] 4.1 Create `src/sitespy/handlers/sites_post.py`
    - Follow existing handler pattern
    - Extract claims, resolve role, require `super_admin`
    - Require `tenant_id` query parameter, verify tenant exists → 404 if not
    - Parse JSON body, validate `site_id`, `site_name`, `latitude`, `longitude`
    - Apply default `timezone=Europe/London` when omitted; validate IANA timezone when provided
    - Call `data.put_site(...)`, catch `ConditionalCheckFailedException` → 409
    - Return 201 with site record
    - _Requirements: 2.1–2.14_

  - [x] 4.2 Write unit tests for sites_post handler
    - Test happy-path creation → 201
    - Test missing tenant_id query param → 400
    - Test non-existent tenant → 404
    - Test duplicate site_id → 409
    - Test invalid lat/lon → 400
    - Test invalid timezone → 400
    - Test default timezone applied
    - Create `tests/sitespy/test_sites_post.py`
    - _Requirements: 2.1–2.14_

- [x] 5. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Implement POST /v1/sites/{site_id}/cameras handler
  - [x] 6.1 Create `src/sitespy/handlers/cameras_post.py`
    - Follow existing handler pattern
    - Extract claims, resolve role, require `super_admin`
    - Require `tenant_id` query param, extract `site_id` from path
    - Verify site exists → 404 if not
    - Parse JSON body, validate `camera_id`, `camera_name`, optional `camera_model`
    - Generate credential pair via `credentials.generate_credential_pair()`
    - Call `data.put_camera(...)`, catch `ConditionalCheckFailedException` → 409
    - Store credentials in Secrets Manager at `sitespy/cameras/<tenant_id>/<site_id>/<camera_id>`
    - If Secrets Manager fails → rollback DynamoDB write (delete camera) → 500
    - Return 201 with `camera_id`, `ingest_credentials`, `ingest_url`, `ingest_headers`
    - _Requirements: 3.1–3.16_

  - [x] 6.2 Write unit tests for cameras_post handler
    - Test happy-path creation → 201 with credentials
    - Test duplicate camera_id → 409
    - Test non-existent site → 404
    - Test Secrets Manager failure triggers DynamoDB rollback → 500
    - Test credentials format in response
    - Create `tests/sitespy/test_cameras_post.py`
    - _Requirements: 3.1–3.16_

- [x] 7. Implement POST /v1/users handler
  - [x] 7.1 Create `src/sitespy/handlers/users_post.py`
    - Follow existing handler pattern
    - Extract claims, resolve role, require `tenant_admin` or `super_admin`
    - Tenant admin: scope to own tenant_id from JWT; reject cross-tenant or super_admin creation → 403
    - Super admin: use `tenant_id` from request body
    - Validate `email`, `full_name`, `role` (one of `user`, `tenant_admin`, `super_admin`)
    - If role is `user`, require non-empty `site_access` list; validate each site_id exists for tenant
    - Call Cognito `AdminCreateUser` with attributes (`custom:tenant_id`, `custom:site_access`)
    - If role is `tenant_admin`, call `AdminAddUserToGroup(TenantAdmins)`
    - Handle `UsernameExistsException` → 409, `InvalidParameterException` → 400
    - Return 201 with `sub`, `email`, `full_name`, `tenant_id`, `role`, `site_access`
    - _Requirements: 4.1–4.17_

  - [x] 7.2 Write property tests for users_post handler
    - **Property 6: Tenant admin cross-tenant isolation**
    - **Property 13: site_access serialization round-trip**
    - **Validates: Requirements 4.4, 4.14**
    - Create `tests/sitespy/test_users_post_properties.py`

  - [x] 7.3 Write unit tests for users_post handler
    - Test happy-path user creation → 201
    - Test tenant admin cannot create super_admin → 403
    - Test tenant admin cannot create cross-tenant user → 403
    - Test duplicate email → 409
    - Test missing site_access for role=user → 400
    - Test invalid site_id in site_access → 400
    - Create `tests/sitespy/test_users_post.py`
    - _Requirements: 4.1–4.17_

- [x] 8. Implement GET /v1/sites/{site_id}/cameras handler
  - [x] 8.1 Create `src/sitespy/handlers/cameras_get.py`
    - Follow existing handler pattern
    - Extract claims, resolve role, require `tenant_admin` or `super_admin`
    - Tenant admin: resolve tenant_id from JWT; Super admin: require `tenant_id` query param
    - Verify site exists → 404 if not
    - Query cameras using `data.get_cameras_for_site(tenant_id, site_id)`
    - Return 200 with camera list (`camera_id`, `camera_name`, `camera_model`) — no credentials
    - Return empty array when no cameras exist
    - _Requirements: 5.1–5.10_

  - [x] 8.2 Write property tests for cameras_get handler
    - **Property 11: Camera listing never exposes credentials**
    - **Validates: Requirements 5.8, 5.10**
    - Create `tests/sitespy/test_cameras_get_properties.py`

  - [x] 8.3 Write unit tests for cameras_get handler
    - Test happy-path listing → 200 with cameras array
    - Test empty site → 200 with empty array
    - Test non-existent site → 404
    - Test super admin without tenant_id → 400
    - Test no credential fields in response
    - Create `tests/sitespy/test_cameras_get.py`
    - _Requirements: 5.1–5.10_

- [x] 9. Implement POST /v1/sites/{site_id}/cameras/{camera_id}/rotate-credentials handler
  - [x] 9.1 Create `src/sitespy/handlers/cameras_rotate.py`
    - Follow existing handler pattern
    - Extract claims, resolve role, require `tenant_admin` or `super_admin`
    - Tenant admin: verify site belongs to own tenant → 403 if not
    - Super admin: require `tenant_id` query param
    - Verify camera exists → 404 if not
    - Generate new credential pair
    - Update Secrets Manager secret at `sitespy/cameras/<tenant_id>/<site_id>/<camera_id>`
    - If Secrets Manager update fails → 500 (do not modify camera record)
    - Return 200 with `camera_id`, `ingest_credentials`, `ingest_url`, `ingest_headers`
    - _Requirements: 6.1–6.10_

  - [x] 9.2 Write unit tests for cameras_rotate handler
    - Test happy-path rotation → 200 with new credentials
    - Test non-existent camera → 404
    - Test tenant admin accessing other tenant's camera → 403
    - Test Secrets Manager failure → 500
    - Create `tests/sitespy/test_cameras_rotate.py`
    - _Requirements: 6.1–6.10_

- [x] 10. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Add common handler behaviour (correlation ID and CORS)
  - [x] 11.1 Verify correlation ID and CORS handling across all new handlers
    - Ensure each handler uses `_resolve_correlation_id` matching the existing pattern
    - Ensure `json_response` and `error_response` include CORS headers
    - If `sitespy.http` does not already include CORS headers, update `json_response` and `error_response` to add them
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.9_

  - [x] 11.2 Write property tests for common handler behaviour
    - **Property 7: Correlation ID round-trip**
    - **Property 12: CORS headers present on all responses**
    - **Validates: Requirements 7.1, 7.2, 7.3, 7.9**
    - Create `tests/sitespy/test_common_handler_properties.py`

- [x] 12. Update SAM template with new Lambda resources
  - [x] 12.1 Add Lambda function resources and API Gateway events to `template.yaml`
    - Add `TenantsPostFunction` → POST /v1/tenants with `DynamoDBCrudPolicy`
    - Add `SitesPostFunction` → POST /v1/sites with `DynamoDBCrudPolicy`
    - Add `CamerasPostFunction` → POST /v1/sites/{site_id}/cameras with `DynamoDBCrudPolicy` + Secrets Manager write policy
    - Add `UsersPostFunction` → POST /v1/users with `DynamoDBReadPolicy` + Cognito admin policy
    - Add `CamerasGetFunction` → GET /v1/sites/{site_id}/cameras with `DynamoDBReadPolicy`
    - Add `CamerasRotateFunction` → POST /v1/sites/{site_id}/cameras/{camera_id}/rotate-credentials with `DynamoDBReadPolicy` + Secrets Manager write policy
    - Add `COGNITO_USER_POOL_ID` environment variable to functions that need Cognito access
    - _Requirements: 1.1, 2.1, 3.1, 4.1, 5.1, 6.1_

- [x] 13. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document (13 properties total)
- Unit tests validate specific examples and edge cases
- All handlers follow the established pattern in `tenants.py` and `sites.py` (powertools decorators, correlation ID, ApiError hierarchy)
- Hypothesis is already configured in the project (`.hypothesis/` directory exists)
- Tests go in `tests/sitespy/` directory

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["1.3", "1.4", "2.1"] },
    { "id": 2, "tasks": ["3.1", "4.1"] },
    { "id": 3, "tasks": ["3.2", "3.3", "4.2", "6.1"] },
    { "id": 4, "tasks": ["6.2", "7.1", "8.1"] },
    { "id": 5, "tasks": ["7.2", "7.3", "8.2", "8.3", "9.1"] },
    { "id": 6, "tasks": ["9.2", "11.1"] },
    { "id": 7, "tasks": ["11.2", "12.1"] }
  ]
}
```
