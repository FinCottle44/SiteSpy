# Design Document: Admin Management Endpoints

## Overview

This design covers six new Lambda handlers that implement the admin management API for SiteSpy: tenant creation, site registration, camera registration (with credential minting), user creation, camera listing, and credential rotation. All handlers follow the established patterns in the codebase — aws-lambda-powertools for logging/metrics, the canonical `ApiError` hierarchy for error responses, correlation ID propagation, and the single-table DynamoDB design.

The endpoints are split by HTTP method and resource to keep each Lambda focused and independently deployable:

| Endpoint | Handler | Role Required |
|----------|---------|---------------|
| POST /v1/tenants | `sitespy.handlers.tenants_post.handler` | super_admin |
| POST /v1/sites | `sitespy.handlers.sites_post.handler` | super_admin |
| POST /v1/sites/{site_id}/cameras | `sitespy.handlers.cameras_post.handler` | super_admin |
| POST /v1/users | `sitespy.handlers.users_post.handler` | tenant_admin or super_admin |
| GET /v1/sites/{site_id}/cameras | `sitespy.handlers.cameras_get.handler` | tenant_admin or super_admin |
| POST /v1/sites/{site_id}/cameras/{camera_id}/rotate-credentials | `sitespy.handlers.cameras_rotate.handler` | tenant_admin or super_admin |

## Architecture

```mermaid
flowchart TD
    Client[Dashboard / CLI] -->|Bearer JWT| APIGW[API Gateway + Cognito Authorizer]
    APIGW --> TenantsPost[POST /v1/tenants Lambda]
    APIGW --> SitesPost[POST /v1/sites Lambda]
    APIGW --> CamerasPost[POST /v1/sites/{site_id}/cameras Lambda]
    APIGW --> UsersPost[POST /v1/users Lambda]
    APIGW --> CamerasGet[GET /v1/sites/{site_id}/cameras Lambda]
    APIGW --> CamerasRotate[POST .../rotate-credentials Lambda]

    TenantsPost --> DDB[(DynamoDB)]
    SitesPost --> DDB
    CamerasPost --> DDB
    CamerasPost --> SM[Secrets Manager]
    UsersPost --> DDB
    UsersPost --> Cognito[Cognito User Pool]
    CamerasGet --> DDB
    CamerasRotate --> SM
    CamerasRotate --> DDB
```

Each Lambda:
1. Extracts claims from `event.requestContext.authorizer.claims`
2. Resolves the caller's role from `cognito:groups`
3. Enforces role-based access (raises `Forbidden` on failure)
4. Validates the request body/params (raises `BadRequest` on failure)
5. Performs the business operation (DynamoDB write, Secrets Manager call, Cognito call)
6. Returns a structured JSON response via `sitespy.http.json_response`

All Lambdas share the existing `sitespy.errors`, `sitespy.http`, and `sitespy.data` modules. New data-layer functions are added to `sitespy/data.py` for write operations.

## Components and Interfaces

### Shared Auth Module

A new `sitespy.auth` module (or inline helpers matching the existing pattern) provides:

```python
def extract_claims(event: dict) -> dict:
    """Extract JWT claims from API Gateway authorizer context."""

def resolve_caller(claims: dict) -> tuple[str, str | None, list[str]]:
    """Returns (role, tenant_id, site_access)."""

def require_role(role: str, allowed: set[str]) -> None:
    """Raises Forbidden if role not in allowed set."""
```

These mirror the existing `_extract_claims` and `_resolve_caller` in `sites.py` and `tenants.py`. The new handlers can either inline them (matching current style) or import from a shared module.

### Credential Generator

```python
import secrets
import string

def generate_credential_pair() -> tuple[str, str]:
    """Generate a camera ingest credential pair.
    
    Returns:
        (username, password) where:
        - username: 32 chars total, prefix 'sitespy_cam_' + 20 random alphanumeric
        - password: 48 random alphanumeric chars (mixed case + digits)
    """
    alphabet = string.ascii_letters + string.digits
    suffix = ''.join(secrets.choice(alphabet) for _ in range(20))
    username = f"sitespy_cam_{suffix}"
    password = ''.join(secrets.choice(alphabet) for _ in range(48))
    return username, password
```

### Validation Helpers

A `sitespy.validation` module (or inline per handler) provides field-level validators:

```python
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TENANT_ID_RE = re.compile(r'^[a-z0-9_]{3,32}$')
SITE_ID_RE = re.compile(r'^[a-z0-9_]{1,64}$')
CAMERA_ID_RE = re.compile(r'^[a-z0-9_]{1,64}$')
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

def validate_tenant_id(value: str) -> bool: ...
def validate_site_id(value: str) -> bool: ...
def validate_camera_id(value: str) -> bool: ...
def validate_email(value: str) -> bool: ...
def validate_latitude(value: float) -> bool: ...  # [-90, 90]
def validate_longitude(value: float) -> bool: ...  # [-180, 180]
def validate_timezone(value: str) -> bool: ...  # ZoneInfo lookup
```

### Handler: POST /v1/tenants (`tenants_post.handler`)

1. Resolve role → require `super_admin`
2. Parse JSON body → validate `tenant_id`, `tenant_name`, `primary_contact_email`, optional `stale_threshold_hours`
3. `data.put_tenant(...)` with ConditionExpression `attribute_not_exists(PK)` → Conflict on failure
4. Return 201 with tenant record

### Handler: POST /v1/sites (`sites_post.handler`)

1. Resolve role → require `super_admin`
2. Extract `tenant_id` from query params → validate exists in DDB
3. Parse JSON body → validate `site_id`, `site_name`, `latitude`, `longitude`, optional `timezone`
4. `data.put_site(...)` with ConditionExpression → Conflict on duplicate
5. Return 201 with site record

### Handler: POST /v1/sites/{site_id}/cameras (`cameras_post.handler`)

1. Resolve role → require `super_admin`
2. Extract `tenant_id` from query params, `site_id` from path
3. Verify site exists → 404 if not
4. Parse JSON body → validate `camera_id`, `camera_name`, optional `camera_model`
5. Generate credential pair
6. `data.put_camera(...)` with ConditionExpression → Conflict on duplicate
7. Store credentials in Secrets Manager
8. If Secrets Manager fails → rollback DynamoDB write → 500
9. Return 201 with camera record + credentials (shown once)

### Handler: POST /v1/users (`users_post.handler`)

1. Resolve role → require `tenant_admin` or `super_admin`
2. Determine target tenant:
   - Tenant admin: use own `custom:tenant_id` from JWT
   - Super admin: use `tenant_id` from request body
3. Enforce tenant admin cannot create cross-tenant or super_admin users
4. Parse JSON body → validate `email`, `full_name`, `role`, optional `site_access`
5. If role is `user`, validate each site_id in `site_access` exists for the tenant
6. Call Cognito `AdminCreateUser` with attributes
7. If role is `tenant_admin`, call `AdminAddUserToGroup(TenantAdmins)`
8. Return 201 with user record

### Handler: GET /v1/sites/{site_id}/cameras (`cameras_get.handler`)

1. Resolve role → require `tenant_admin` or `super_admin`
2. Resolve tenant_id (from JWT for tenant admin, from query param for super admin)
3. Verify site exists → 404 if not
4. Query cameras using existing `data.get_cameras_for_site()`
5. Return 200 with camera list (no credentials)

### Handler: POST /v1/sites/{site_id}/cameras/{camera_id}/rotate-credentials (`cameras_rotate.handler`)

1. Resolve role → require `tenant_admin` or `super_admin`
2. Resolve tenant_id, verify site/camera exists → 404 if not
3. Tenant admin: verify site belongs to own tenant → 403 if not
4. Generate new credential pair
5. Update Secrets Manager secret (PutSecretValue or UpdateSecret)
6. Return 200 with new credentials (shown once)

## Data Models

### DynamoDB Items

All items live in the existing `sitespy-{env}-data` table.

**Tenant Record** (already exists for GET, now also written by POST):

| Attribute | Type | Example |
|-----------|------|---------|
| PK | S | `TENANT#acme_corp` |
| SK | S | `TENANT#acme_corp` |
| tenant_name | S | `Acme Construction Ltd` |
| primary_contact_email | S | `ops@acme.example.com` |
| stale_threshold_hours | N | `24` |
| created_at | S | `2025-06-15T10:00:00Z` |

**Site Record** (already exists for GET, now also written by POST):

| Attribute | Type | Example |
|-----------|------|---------|
| PK | S | `TENANT#acme_corp` |
| SK | S | `SITE#site_001` |
| site_name | S | `Acme Tower — Phase 2` |
| latitude | N | `51.5074` |
| longitude | N | `-0.1278` |
| timezone | S | `Europe/London` |
| address | S | `1 Example Street, London` (optional) |
| created_at | S | `2025-06-15T10:00:00Z` |

**Camera Record** (already exists for GET, now also written by POST):

| Attribute | Type | Example |
|-----------|------|---------|
| PK | S | `TENANT#acme_corp` |
| SK | S | `SITE#site_001#CAM#cam_01` |
| camera_name | S | `North elevation` |
| camera_model | S | `Axis P1455-LE` |
| created_at | S | `2025-06-15T10:00:00Z` |

**Secrets Manager Secret** (at `sitespy/cameras/<tenant_id>/<site_id>/<camera_id>`):

```json
{
  "username": "sitespy_cam_8f2a4b9c1d7e3k5m",
  "password": "Qr9vL3kP7mN2xB8jY4hT5wF6sD1gA0cEaB2dF4gH6jK8lM",
  "tenant_id": "acme_corp",
  "site_id": "site_001",
  "camera_id": "cam_01"
}
```

### Request/Response Schemas

**POST /v1/tenants — Request:**
```json
{
  "tenant_id": "acme_corp",
  "tenant_name": "Acme Construction Ltd",
  "primary_contact_email": "ops@acme.example.com",
  "stale_threshold_hours": 24
}
```

**POST /v1/tenants — Response (201):**
```json
{
  "tenant_id": "acme_corp",
  "tenant_name": "Acme Construction Ltd",
  "primary_contact_email": "ops@acme.example.com",
  "stale_threshold_hours": 24
}
```

**POST /v1/sites?tenant_id=acme_corp — Request:**
```json
{
  "site_id": "site_001",
  "site_name": "Acme Tower — Phase 2",
  "latitude": 51.5074,
  "longitude": -0.1278,
  "timezone": "Europe/London"
}
```

**POST /v1/sites — Response (201):**
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

**POST /v1/sites/{site_id}/cameras?tenant_id=acme_corp — Request:**
```json
{
  "camera_id": "cam_01",
  "camera_name": "North elevation",
  "camera_model": "Axis P1455-LE"
}
```

**POST /v1/sites/{site_id}/cameras — Response (201):**
```json
{
  "camera_id": "cam_01",
  "ingest_credentials": {
    "username": "sitespy_cam_8f2a4b9c1d7e3k5m",
    "password": "Qr9vL3kP7mN2xB8jY4hT5wF6sD1gA0cEaB2dF4gH6jK8lM"
  },
  "ingest_url": "https://<api_id>.execute-api.eu-west-2.amazonaws.com/prod/v1/ingest?cameraID=cam_01",
  "ingest_headers": {
    "X-Tenant-ID": "acme_corp",
    "X-Site-ID": "site_001"
  }
}
```

**POST /v1/users — Request:**
```json
{
  "email": "jane.doe@acme.example.com",
  "full_name": "Jane Doe",
  "tenant_id": "acme_corp",
  "role": "user",
  "site_access": ["site_001", "site_002"]
}
```

**POST /v1/users — Response (201):**
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

**GET /v1/sites/{site_id}/cameras?tenant_id=acme_corp — Response (200):**
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

**POST /v1/sites/{site_id}/cameras/{camera_id}/rotate-credentials — Response (200):**
```json
{
  "camera_id": "cam_01",
  "ingest_credentials": {
    "username": "sitespy_cam_newRandom20chars",
    "password": "newRandom48charsHere..."
  },
  "ingest_url": "https://<api_id>.execute-api.eu-west-2.amazonaws.com/prod/v1/ingest?cameraID=cam_01",
  "ingest_headers": {
    "X-Tenant-ID": "acme_corp",
    "X-Site-ID": "site_001"
  }
}
```

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Role enforcement rejects unauthorized callers

*For any* admin endpoint with a required role set, and *for any* JWT claims where the caller's resolved role is not in the allowed set, the handler SHALL return a 403 response with error key `ACCESS_DENIED` and SHALL NOT perform any write operation.

**Validates: Requirements 1.2, 2.2, 3.2, 4.2, 5.2, 6.2**

### Property 2: ID format validation rejects non-conforming identifiers

*For any* string that does not match the required pattern (`^[a-z0-9_]{3,32}$` for tenant_id, `^[a-z0-9_]{1,64}$` for site_id and camera_id), when submitted as the respective ID field, the handler SHALL return a 400 response with error key `BAD_REQUEST`.

**Validates: Requirements 1.4, 2.6, 3.6**

### Property 3: Missing required fields produce 400

*For any* request body that is missing at least one required field for the target endpoint, the handler SHALL return a 400 response with error key `BAD_REQUEST` and a message identifying the missing field(s).

**Validates: Requirements 1.3, 2.9, 3.9, 7.8**

### Property 4: Credential pair generation format invariant

*For any* invocation of the credential generation function, the returned username SHALL be exactly 32 characters with prefix `sitespy_cam_` followed by 20 alphanumeric characters, and the returned password SHALL be exactly 48 alphanumeric characters (mixed case + digits).

**Validates: Requirements 3.10, 6.6**

### Property 5: Valid creation round-trip preserves all input fields

*For any* valid tenant/site/camera creation request, the 201 response body SHALL contain all input fields with values matching the submitted request (tenant_id, tenant_name, primary_contact_email, stale_threshold_hours for tenants; site_id, site_name, tenant_id, latitude, longitude, timezone for sites).

**Validates: Requirements 1.11, 2.14**

### Property 6: Tenant admin cross-tenant isolation

*For any* tenant admin with `custom:tenant_id` = X, and *for any* request that targets a resource belonging to tenant Y where X ≠ Y, the handler SHALL return a 403 response with error key `ACCESS_DENIED`.

**Validates: Requirements 4.4, 6.3**

### Property 7: Correlation ID round-trip

*For any* request where the `X-Correlation-Id` header matches `^[A-Za-z0-9_-]{1,128}$`, the response SHALL echo that exact value in the `X-Correlation-Id` response header. *For any* request where the header is absent or invalid, the response SHALL contain a valid UUID v4 in the `X-Correlation-Id` response header.

**Validates: Requirements 7.1, 7.2, 7.3**

### Property 8: Invalid JSON body produces 400

*For any* request body that is not valid JSON (random byte sequences, truncated JSON, XML, etc.), the handler SHALL return a 400 response with error key `BAD_REQUEST`.

**Validates: Requirements 7.7**

### Property 9: Latitude/longitude range validation

*For any* latitude value outside [-90, 90] or *for any* longitude value outside [-180, 180], the site creation handler SHALL return a 400 response with error key `BAD_REQUEST`.

**Validates: Requirements 2.8**

### Property 10: stale_threshold_hours range validation

*For any* `stale_threshold_hours` value that is not an integer in [1, 720], the tenant creation handler SHALL return a 400 response with error key `BAD_REQUEST`.

**Validates: Requirements 1.8**

### Property 11: Camera listing never exposes credentials

*For any* GET /v1/sites/{site_id}/cameras response, and *for any* camera record in the backing store, the response body SHALL NOT contain any field named `username`, `password`, `ingest_credentials`, or any credential-like data.

**Validates: Requirements 5.8, 5.10**

### Property 12: CORS headers present on all responses

*For any* request to any admin endpoint (success or error), the response SHALL include `Access-Control-Allow-Origin`, `Access-Control-Allow-Headers`, and `Access-Control-Allow-Methods` headers.

**Validates: Requirements 7.9**

### Property 13: site_access serialization round-trip

*For any* non-empty list of valid site IDs, when creating a user with role `user`, the `custom:site_access` Cognito attribute SHALL be set to the comma-separated join of those IDs, and parsing that string back by splitting on commas SHALL produce the original list.

**Validates: Requirements 4.14**

## Error Handling

All handlers follow the established error pattern from the existing codebase:

1. **Structured exceptions**: Business logic raises `ApiError` subclasses (`BadRequest`, `Forbidden`, `NotFound`, `Conflict`, `InternalError`). The outer handler catches these and calls `error_response()`.

2. **Unhandled exceptions**: A bare `except Exception` block catches anything unexpected, logs the full traceback via `logger.exception()`, and returns `unhandled_error_response()` (500 with generic message).

3. **Rollback on partial failure** (camera creation only): If Secrets Manager write fails after DynamoDB write succeeds, the handler deletes the DynamoDB camera record before returning 500. This prevents orphaned records.

4. **Cognito error mapping** (user creation):
   - `UsernameExistsException` → 409 CONFLICT
   - `InvalidParameterException` → 400 BAD_REQUEST
   - Other Cognito errors → 500 INTERNAL_ERROR (logged)

5. **DynamoDB ConditionalCheckFailedException**: Mapped to 409 CONFLICT for duplicate tenant/site/camera creation attempts.

6. **Validation errors**: Collected per-field and returned in a single 400 response with a descriptive message listing all failing fields (fail-fast on first error is also acceptable given the existing handler style).

## Testing Strategy

### Unit Tests (pytest)

Unit tests cover specific examples, edge cases, and integration points:

- Happy-path creation for each endpoint (tenant, site, camera, user)
- Duplicate creation → 409
- Missing required fields → 400
- Non-existent parent resource → 404
- Secrets Manager failure rollback (camera creation)
- Cognito `UsernameExistsException` → 409
- Default values (stale_threshold_hours=24, timezone=Europe/London)
- Tenant admin cannot create super_admin users
- Super admin without tenant_id query param → 400

### Property-Based Tests (Hypothesis)

Property-based tests verify universal properties across generated inputs. Each test runs a minimum of 100 iterations.

**Library**: [Hypothesis](https://hypothesis.readthedocs.io/) (already present in the project's `.hypothesis/` directory)

**Configuration**: Each property test uses `@settings(max_examples=100)` minimum.

**Tag format**: Each test includes a docstring comment referencing the design property:
```python
# Feature: admin-management-endpoints, Property 1: Role enforcement rejects unauthorized callers
```

**Properties to implement**:

| Property | Test Focus | Key Generators |
|----------|-----------|----------------|
| 1 | Role enforcement | Random claims without required groups |
| 2 | ID format validation | Random strings violating regex patterns |
| 3 | Missing required fields | Random subsets of required field sets |
| 4 | Credential generation | Call generator N times, assert format |
| 5 | Creation round-trip | Random valid inputs, assert response matches |
| 6 | Tenant admin isolation | Random tenant pairs where X ≠ Y |
| 7 | Correlation ID round-trip | Random valid/invalid correlation IDs |
| 8 | Invalid JSON body | Random non-JSON byte sequences |
| 9 | Lat/lon range validation | Random floats outside valid ranges |
| 10 | stale_threshold_hours | Random values outside [1, 720] |
| 11 | Camera listing no credentials | Random camera items, assert no credential fields |
| 12 | CORS headers | Random requests, assert headers present |
| 13 | site_access serialization | Random site ID lists, assert round-trip |

### Integration Tests

Integration tests (run against LocalStack or a dev-deployed stack) cover:

- End-to-end tenant → site → camera → user provisioning flow
- Secrets Manager credential storage and retrieval
- Cognito user creation and group assignment
- DynamoDB ConditionExpression enforcement
- API Gateway routing and authorizer integration

### Test Infrastructure

- **Mocking**: `unittest.mock.patch` for boto3 clients (DynamoDB, Secrets Manager, Cognito)
- **Fixtures**: Shared pytest fixtures for valid events, claims, and DynamoDB responses
- **CI**: Tests run in GitHub Actions on every PR; property tests use a fixed seed for reproducibility in CI

