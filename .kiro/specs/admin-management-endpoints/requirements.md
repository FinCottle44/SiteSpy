# Requirements Document

## Introduction

Admin management endpoints for the SiteSpy platform. These endpoints allow super admins to provision tenants, sites, and cameras, and allow tenant admins to manage users within their own tenant, list cameras, and rotate camera credentials when a device needs reconfiguring. All endpoints are implemented as AWS Lambda functions (Python 3.12) deployed via SAM, following existing handler patterns (aws-lambda-powertools, canonical error envelope, correlation ID propagation).

## Glossary

- **Admin_API**: The set of Lambda-backed REST endpoints under `/v1/` that handle resource creation and management operations.
- **Super_Admin**: A user in the `SuperAdmins` Cognito group with platform-wide access across all tenants.
- **Tenant_Admin**: A user in the `TenantAdmins` Cognito group with access to all resources within their own tenant.
- **Site_Mapping_Table**: The DynamoDB single-table storing tenant, site, and camera records with PK=`TENANT#<tenant_id>` and various SK patterns.
- **Secrets_Manager**: AWS Secrets Manager, used to store per-camera ingest credentials at path `sitespy/cameras/<tenant_id>/<site_id>/<camera_id>`.
- **Cognito_Admin_SDK**: The AWS Cognito Identity Provider admin API used to create and manage user accounts.
- **Credential_Pair**: A randomly generated username (32 chars, prefix `sitespy_cam_`) and password (48 chars) used for camera HTTP Basic Auth ingest.
- **Correlation_ID**: A request-scoped identifier (from X-Correlation-Id header or generated UUID) included in all responses and logs.

## Requirements

### Requirement 1: Create Tenant

**User Story:** As a super admin, I want to create a new tenant, so that I can onboard a new construction company onto the platform.

#### Acceptance Criteria

1. WHEN a valid tenant creation request is received at POST /v1/tenants, THE Admin_API SHALL create a tenant record in the Site_Mapping_Table with PK=`TENANT#<tenant_id>` and SK=`TENANT#<tenant_id>`.
2. IF the caller does not have the `super_admin` role, THEN THE Admin_API SHALL return a 403 response with error key `ACCESS_DENIED`.
3. THE Admin_API SHALL require `tenant_id`, `tenant_name`, and `primary_contact_email` as mandatory fields in the request body.
4. THE Admin_API SHALL validate that `tenant_id` matches the pattern `^[a-z0-9_]{3,32}$`.
5. THE Admin_API SHALL validate that `tenant_name` is a non-empty string of at most 128 characters.
6. THE Admin_API SHALL validate that `primary_contact_email` is a string of at most 254 characters conforming to a valid email format.
7. WHEN `stale_threshold_hours` is omitted, THE Admin_API SHALL default the value to 24.
8. WHEN `stale_threshold_hours` is provided, THE Admin_API SHALL validate that the value is an integer between 1 and 720 inclusive.
9. IF a tenant with the same `tenant_id` already exists, THEN THE Admin_API SHALL return a 409 response with error key `CONFLICT`.
10. IF any request body field fails validation, THEN THE Admin_API SHALL return a 400 response with error key `BAD_REQUEST` and a message describing which field failed validation.
11. WHEN the tenant is created successfully, THE Admin_API SHALL return a 201 response containing `tenant_id`, `tenant_name`, `primary_contact_email`, and `stale_threshold_hours`.

### Requirement 2: Register Site

**User Story:** As a super admin, I want to register a new site under a tenant, so that cameras can be provisioned and begin ingesting snapshots.

#### Acceptance Criteria

1. WHEN a POST request is received at /v1/sites, THE Admin_API SHALL route the request to the create-site handler.
2. IF the caller does not have the `super_admin` role, THEN THE Admin_API SHALL return a 403 response with error key `ACCESS_DENIED`.
3. THE Admin_API SHALL require `tenant_id` as a query parameter.
4. IF the `tenant_id` query parameter is missing, THEN THE Admin_API SHALL return a 400 response with error key `BAD_REQUEST`.
5. IF the specified tenant does not exist in the Site_Mapping_Table, THEN THE Admin_API SHALL return a 404 response with error key `NOT_FOUND`.
6. THE Admin_API SHALL validate that `site_id` is a non-empty string matching the pattern `^[a-z0-9_]{1,64}$`.
7. THE Admin_API SHALL validate that `site_name` is a non-empty string of at most 128 characters.
8. THE Admin_API SHALL validate that `latitude` is a number in the range [-90, 90] and `longitude` is a number in the range [-180, 180].
9. IF any required field (`site_id`, `site_name`, `latitude`, or `longitude`) is missing from the request body, THEN THE Admin_API SHALL return a 400 response with error key `BAD_REQUEST`.
10. WHEN `timezone` is omitted from the request body, THE Admin_API SHALL default timezone to `Europe/London`.
11. IF `timezone` is provided, THE Admin_API SHALL validate that it is a valid IANA timezone identifier.
12. WHEN a valid site creation request is received, THE Admin_API SHALL write a site record to the Site_Mapping_Table with PK=`TENANT#<tenant_id>` and SK=`SITE#<site_id>` using a ConditionExpression to enforce uniqueness within the tenant.
13. IF a site with the same `site_id` already exists within the tenant, THEN THE Admin_API SHALL return a 409 response with error key `CONFLICT`.
14. WHEN the site record is written successfully, THE Admin_API SHALL return a 201 response containing `site_id`, `site_name`, `tenant_id`, `latitude`, `longitude`, and `timezone`.

### Requirement 3: Register Camera

**User Story:** As a super admin, I want to register a new camera on a site and receive its ingest credentials, so that I can configure the physical camera device to push snapshots.

#### Acceptance Criteria

1. WHEN a POST request is received at /v1/sites/{site_id}/cameras, THE Admin_API SHALL route the request to the create-camera handler.
2. IF the caller does not have the `super_admin` role, THEN THE Admin_API SHALL return a 403 response with error key `ACCESS_DENIED`.
3. THE Admin_API SHALL require `tenant_id` as a query parameter.
4. IF the `tenant_id` query parameter is missing, THEN THE Admin_API SHALL return a 400 response with error key `BAD_REQUEST`.
5. IF the specified site does not exist under the given tenant in the Site_Mapping_Table, THEN THE Admin_API SHALL return a 404 response with error key `NOT_FOUND`.
6. THE Admin_API SHALL validate that `camera_id` matches the pattern `^[a-z0-9_]{1,64}$`.
7. THE Admin_API SHALL validate that `camera_name` is a non-empty string of at most 128 characters.
8. `camera_model` is optional; when provided it SHALL be a string of at most 128 characters.
9. IF any required body field (`camera_id`, `camera_name`) is missing or fails validation, THEN THE Admin_API SHALL return a 400 response with error key `BAD_REQUEST`.
10. WHEN a valid camera creation request is received, THE Admin_API SHALL generate a Credential_Pair consisting of a 32-character random username with prefix `sitespy_cam_` (20 random alphanumeric characters) and a 48-character random alphanumeric password.
11. THE Admin_API SHALL write the camera record to the Site_Mapping_Table with PK=`TENANT#<tenant_id>` and SK=`SITE#<site_id>#CAM#<camera_id>` using a ConditionExpression to enforce uniqueness.
12. IF a camera with the same `camera_id` already exists on the site, THEN THE Admin_API SHALL return a 409 response with error key `CONFLICT`.
13. WHEN the camera record is written successfully, THE Admin_API SHALL store the Credential_Pair in Secrets_Manager at path `sitespy/cameras/<tenant_id>/<site_id>/<camera_id>`.
14. IF the Secrets_Manager write fails after the DynamoDB write succeeds, THEN THE Admin_API SHALL delete the camera record from the Site_Mapping_Table and return a 500 response with error key `INTERNAL_ERROR`.
15. WHEN the camera is registered successfully, THE Admin_API SHALL return a 201 response containing `camera_id`, `ingest_credentials` (username and password), `ingest_url` (the full ingest endpoint URL including `cameraID` query parameter), and `ingest_headers` (`X-Tenant-ID` and `X-Site-ID` values).
16. THE Admin_API SHALL return the Credential_Pair in the response exactly once; no subsequent API call SHALL retrieve the plaintext credentials.

### Requirement 4: Create User

**User Story:** As a tenant admin, I want to create users within my tenant, so that team members can access the dashboard and view site cameras.

#### Acceptance Criteria

1. WHEN a POST request is received at /v1/users, THE Admin_API SHALL route the request to the create-user handler.
2. IF the caller does not have the `tenant_admin` or `super_admin` role, THEN THE Admin_API SHALL return a 403 response with error key `ACCESS_DENIED`.
3. WHEN a Tenant_Admin submits a user creation request, THE Admin_API SHALL scope the new user to the caller's own `tenant_id` from the JWT claims.
4. IF a Tenant_Admin attempts to create a user in a different tenant (by supplying a `tenant_id` that differs from their own), THEN THE Admin_API SHALL return a 403 response with error key `ACCESS_DENIED`.
5. IF a Tenant_Admin attempts to create a user with role `super_admin`, THEN THE Admin_API SHALL return a 403 response with error key `ACCESS_DENIED`.
6. THE Admin_API SHALL enforce that only `super_admin` users can create users in a tenant other than their own.
7. THE Admin_API SHALL validate that `email` is a valid email address of at most 254 characters.
8. THE Admin_API SHALL validate that `full_name` is a non-empty string of at most 128 characters.
9. THE Admin_API SHALL validate that `role` is one of `user`, `tenant_admin`, or `super_admin`.
10. WHEN `role` is `user`, THE Admin_API SHALL require the `site_access` field as a non-empty list of site IDs.
11. WHEN `role` is `user`, THE Admin_API SHALL validate that each site ID in `site_access` exists in the Site_Mapping_Table for the target tenant.
12. THE Admin_API SHALL set the `custom:tenant_id` attribute on the Cognito user to the target tenant ID.
13. WHEN `role` is `tenant_admin`, THE Admin_API SHALL add the user to the `TenantAdmins` Cognito group.
14. WHEN `role` is `user`, THE Admin_API SHALL set the `custom:site_access` attribute to a comma-separated list of site IDs.
15. WHEN the user is created successfully, THE Admin_API SHALL return a 201 response containing the Cognito `sub`, `email`, `full_name`, `tenant_id`, `role`, and `site_access`.
16. THE Admin_API SHALL trigger Cognito to send an invitation email with a temporary password to the new user.
17. IF a user with the same email already exists in the User Pool, THEN THE Admin_API SHALL return a 409 response with error key `CONFLICT`.

### Requirement 5: List Cameras for Site

**User Story:** As a tenant admin, I want to list all cameras registered on a site, so that I can review the camera inventory and identify cameras that need attention.

#### Acceptance Criteria

1. WHEN a GET request is received at /v1/sites/{site_id}/cameras, THE Admin_API SHALL route the request to the list-cameras handler.
2. IF the caller does not have the `tenant_admin` or `super_admin` role, THEN THE Admin_API SHALL return a 403 response with error key `ACCESS_DENIED`.
3. WHEN a Tenant_Admin submits the request, THE Admin_API SHALL resolve `tenant_id` from the caller's JWT `custom:tenant_id` claim.
4. WHEN a Super_Admin submits the request, THE Admin_API SHALL require `tenant_id` as a query parameter.
5. IF a Super_Admin submits the request without a `tenant_id` query parameter, THEN THE Admin_API SHALL return a 400 response with error key `BAD_REQUEST`.
6. IF the specified site does not exist under the resolved tenant, THEN THE Admin_API SHALL return a 404 response with error key `NOT_FOUND`.
7. WHEN the site exists, THE Admin_API SHALL query the Site_Mapping_Table for all camera items with PK=`TENANT#<tenant_id>` and SK beginning with `SITE#<site_id>#CAM#`.
8. THE Admin_API SHALL return a 200 response containing an array of camera objects with `camera_id`, `camera_name`, and `camera_model` fields.
9. WHEN the site has no registered cameras, THE Admin_API SHALL return a 200 response with an empty `cameras` array.
10. THE Admin_API SHALL NOT include any credential information in the response.

### Requirement 6: Rotate Camera Credentials

**User Story:** As a tenant admin, I want to rotate camera credentials when a camera needs reconfiguring, so that I can obtain new ingest credentials and invalidate the old ones.

#### Acceptance Criteria

1. WHEN a POST request is received at /v1/sites/{site_id}/cameras/{camera_id}/rotate-credentials, THE Admin_API SHALL route the request to the rotate-credentials handler.
2. IF the caller does not have the `tenant_admin` or `super_admin` role, THEN THE Admin_API SHALL return a 403 response with error key `ACCESS_DENIED`.
3. WHEN a Tenant_Admin submits the request, THE Admin_API SHALL verify the site belongs to the caller's own tenant; IF the site does not belong to the caller's tenant, THEN THE Admin_API SHALL return a 403 response with error key `ACCESS_DENIED`.
4. WHEN a Super_Admin submits the request, THE Admin_API SHALL require `tenant_id` as a query parameter.
5. IF the camera does not exist under the specified site and tenant in the Site_Mapping_Table, THEN THE Admin_API SHALL return a 404 response with error key `NOT_FOUND`.
6. WHEN a valid rotation request is received, THE Admin_API SHALL generate a new Credential_Pair consisting of a 32-character random username with prefix `sitespy_cam_` and a 48-character random password.
7. WHEN the new Credential_Pair is generated, THE Admin_API SHALL update the secret in Secrets_Manager at path `sitespy/cameras/<tenant_id>/<site_id>/<camera_id>`, replacing the previous credentials so that any subsequent ingest request using the old credentials is rejected.
8. WHEN the credentials are rotated successfully, THE Admin_API SHALL return a 200 response containing `camera_id`, `ingest_credentials` (username and password), `ingest_url`, and `ingest_headers`.
9. THE Admin_API SHALL return the new Credential_Pair in the response exactly once; no subsequent API call SHALL retrieve the plaintext credentials.
10. IF the Secrets_Manager update fails, THEN THE Admin_API SHALL return a 500 response with error key `INTERNAL_ERROR` and SHALL NOT modify the camera record.

### Requirement 7: Common Handler Behaviour

**User Story:** As a developer, I want all admin endpoints to follow consistent patterns, so that the API is predictable and observable.

#### Acceptance Criteria

1. WHEN the X-Correlation-Id request header is present and matches `^[A-Za-z0-9_-]{1,128}$`, THE admin handlers SHALL use that value as the Correlation_ID for the request.
2. IF the X-Correlation-Id header is absent or does not match `^[A-Za-z0-9_-]{1,128}$`, THEN THE admin handlers SHALL generate a new UUID v4 as the Correlation_ID.
3. THE admin handlers SHALL include the Correlation_ID in the X-Correlation-Id response header on all responses including error responses.
4. THE admin handlers SHALL use aws-lambda-powertools Logger with structured JSON logging including the fields `correlation_id`, `route`, `status_code`, and `latency_ms` on every request completion.
5. THE admin handlers SHALL use aws-lambda-powertools Metrics to emit a count metric on successful responses and a count metric on error responses, each with a `route` dimension.
6. IF an unhandled exception occurs, THEN THE admin handlers SHALL return a 500 response with error key `INTERNAL_ERROR` and a generic message, and SHALL log the exception including the full stack trace.
7. IF the request body is not valid JSON, THEN THE admin handlers SHALL return a 400 response with error key `BAD_REQUEST` and a message indicating the parse failure.
8. IF the request body is valid JSON but missing required fields or contains fields that fail validation, THEN THE admin handlers SHALL return a 400 response with error key `BAD_REQUEST` and a message indicating which fields failed validation.
9. THE admin handlers SHALL include CORS headers on all responses: `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Headers: Content-Type, Authorization, X-Correlation-Id`, and `Access-Control-Allow-Methods: GET, POST, PATCH, OPTIONS`.
