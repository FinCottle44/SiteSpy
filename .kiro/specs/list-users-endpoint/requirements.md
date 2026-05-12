# Requirements Document

## Introduction

A "List Users" endpoint for the SiteSpy admin management API. This feature adds a GET /v1/users endpoint that returns all user records for a tenant, and modifies the existing POST /v1/users handler to persist user records to DynamoDB at creation time. User records are stored with PK=TENANT#<tenant_id> and SK=USER#<sub>, enabling efficient per-tenant queries without calling Cognito directly.

## Glossary

- **Admin_API**: The set of Lambda-backed REST endpoints under `/v1/` that handle resource creation and management operations.
- **Super_Admin**: A user in the `SuperAdmins` Cognito group with platform-wide access across all tenants.
- **Tenant_Admin**: A user in the `TenantAdmins` Cognito group with access to all resources within their own tenant.
- **Site_Mapping_Table**: The DynamoDB single-table storing tenant, site, camera, and user records with PK=`TENANT#<tenant_id>` and various SK patterns.
- **User_Record**: A DynamoDB item representing a user, stored with PK=`TENANT#<tenant_id>` and SK=`USER#<sub>`, containing sub, email, full_name, tenant_id, role, and site_access fields.
- **Correlation_ID**: A request-scoped identifier (from X-Correlation-Id header or generated UUID) included in all responses and logs.

## Requirements

### Requirement 1: Persist User Record on Creation

**User Story:** As a platform operator, I want user records written to DynamoDB at creation time, so that the List Users endpoint can query users without calling Cognito directly.

#### Acceptance Criteria

1. WHEN a user is created successfully via POST /v1/users, THE Admin_API SHALL write a User_Record to the Site_Mapping_Table with PK=`TENANT#<tenant_id>` and SK=`USER#<sub>`.
2. THE Admin_API SHALL store the following attributes in the User_Record: `sub`, `email`, `full_name`, `tenant_id`, `role`, and `site_access`.
3. WHEN the user's role is `user`, THE Admin_API SHALL store `site_access` as a list of site ID strings in the User_Record.
4. WHEN the user's role is `tenant_admin` or `super_admin`, THE Admin_API SHALL store `site_access` as an empty list in the User_Record.
5. THE Admin_API SHALL write the User_Record after the Cognito user creation and group assignment succeed.
6. IF the DynamoDB write for the User_Record fails after Cognito user creation succeeds, THEN THE Admin_API SHALL log the failure and return a 500 response with error key `INTERNAL_ERROR`.

### Requirement 2: List Users Endpoint

**User Story:** As a tenant admin, I want to list all users in my tenant, so that I can review who has access to the platform.

#### Acceptance Criteria

1. WHEN a GET request is received at /v1/users, THE Admin_API SHALL route the request to the list-users handler.
2. IF the caller does not have the `tenant_admin` or `super_admin` role, THEN THE Admin_API SHALL return a 403 response with error key `ACCESS_DENIED`.
3. WHEN a Tenant_Admin submits the request, THE Admin_API SHALL resolve `tenant_id` from the caller's JWT `custom:tenant_id` claim.
4. WHEN a Super_Admin submits the request, THE Admin_API SHALL require `tenant_id` as a query parameter.
5. IF a Super_Admin submits the request without a `tenant_id` query parameter, THEN THE Admin_API SHALL return a 400 response with error key `BAD_REQUEST`.
6. WHEN the tenant_id is resolved, THE Admin_API SHALL query the Site_Mapping_Table for all items with PK=`TENANT#<tenant_id>` and SK beginning with `USER#`.
7. THE Admin_API SHALL return a 200 response containing a `users` array where each element includes `sub`, `email`, `full_name`, `tenant_id`, `role`, and `site_access`.
8. WHEN the tenant has no user records in DynamoDB, THE Admin_API SHALL return a 200 response with an empty `users` array.
9. THE Admin_API SHALL return all user records for the tenant in a single response without pagination.

### Requirement 3: List Users Common Handler Behaviour

**User Story:** As a developer, I want the List Users endpoint to follow the same patterns as other admin endpoints, so that the API remains consistent and observable.

#### Acceptance Criteria

1. WHEN the X-Correlation-Id request header is present and matches `^[A-Za-z0-9_-]{1,128}$`, THE list-users handler SHALL use that value as the Correlation_ID for the request.
2. IF the X-Correlation-Id header is absent or does not match `^[A-Za-z0-9_-]{1,128}$`, THEN THE list-users handler SHALL generate a new UUID v4 as the Correlation_ID.
3. THE list-users handler SHALL include the Correlation_ID in the X-Correlation-Id response header on all responses including error responses.
4. THE list-users handler SHALL use aws-lambda-powertools Logger with structured JSON logging including the fields `correlation_id`, `route`, `status_code`, and `latency_ms` on every request completion.
5. THE list-users handler SHALL use aws-lambda-powertools Metrics to emit a count metric on successful responses and a count metric on error responses, each with a `route` dimension.
6. IF an unhandled exception occurs, THEN THE list-users handler SHALL return a 500 response with error key `INTERNAL_ERROR` and a generic message, and SHALL log the exception including the full stack trace.
7. THE list-users handler SHALL include CORS headers on all responses: `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Headers: Content-Type, Authorization, X-Correlation-Id`, and `Access-Control-Allow-Methods: GET, POST, PATCH, OPTIONS`.
