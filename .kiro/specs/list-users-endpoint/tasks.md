# Implementation Plan: List Users Endpoint

## Overview

Add a GET /v1/users Lambda handler and modify the existing POST /v1/users handler to persist User_Records to DynamoDB. The data layer gets two new functions (`put_user`, `get_users_for_tenant`), the SAM template gets a new Lambda resource, and both handlers get unit and property-based tests.

## Tasks

- [x] 1. Add data layer functions for user records
  - [x] 1.1 Add `build_user_sk()`, `put_user()`, and `get_users_for_tenant()` to `sitespy/data.py`
    - Add `build_user_sk(sub)` returning `USER#<sub>`
    - Add `put_user(tenant_id, sub, email, full_name, role, site_access)` writing a User_Record with PK=`TENANT#<tenant_id>`, SK=`USER#<sub>` and all six attributes
    - Add `get_users_for_tenant(tenant_id)` querying PK=`TENANT#<tenant_id>` with SK begins_with `USER#`, paginating through all results
    - _Requirements: 1.1, 1.2, 2.6_

  - [x] 1.2 Write unit tests for `put_user` and `get_users_for_tenant` in `tests/sitespy/test_data.py`
    - Test `put_user` writes correct item structure
    - Test `get_users_for_tenant` returns all user items for a tenant
    - Test `get_users_for_tenant` returns empty list when no users exist
    - _Requirements: 1.1, 1.2, 2.6, 2.8_

- [x] 2. Modify POST /v1/users to persist User_Record
  - [x] 2.1 Add DynamoDB `put_user()` call to `users_post.py` after Cognito success
    - After Cognito user creation and group assignment succeed, call `data.put_user()` with tenant_id, sub, email, full_name, role, and site_access
    - If DynamoDB write fails, log the exception and raise `InternalError()`
    - Pass `site_access` list for role=user, empty list for tenant_admin/super_admin
    - Update the `UsersPostFunction` IAM policy in `template.yaml` from `DynamoDBReadPolicy` to `DynamoDBCrudPolicy`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

  - [x] 2.2 Write unit tests for the modified POST /v1/users DynamoDB write in `tests/sitespy/test_users_post.py`
    - Test successful user creation writes User_Record to DynamoDB
    - Test DynamoDB write failure after Cognito success returns 500
    - Test site_access stored as list for role=user
    - Test site_access stored as empty list for tenant_admin/super_admin
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

  - [x] 2.3 Write property test for user record write completeness
    - **Property 3: User record write completeness**
    - **Validates: Requirements 1.1, 1.2**

  - [x] 2.4 Write property test for site_access storage invariant
    - **Property 4: site_access storage invariant**
    - **Validates: Requirements 1.3, 1.4**

- [x] 3. Checkpoint - Ensure data layer and POST modifications pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Create GET /v1/users handler
  - [x] 4.1 Create `sitespy/handlers/users_get.py` following the `cameras_get.py` pattern
    - Implement `handler()` with powertools Logger/Metrics decorators, correlation ID, latency tracking
    - Implement `_handle()` with role enforcement (tenant_admin or super_admin only)
    - Resolve tenant_id from JWT `custom:tenant_id` for tenant_admin, from `tenant_id` query param for super_admin
    - Return 400 if super_admin omits `tenant_id` query param
    - Call `data.get_users_for_tenant(tenant_id)` and map items to response objects
    - Implement `_map_user_item()` extracting sub, email, full_name, tenant_id, role, site_access from DynamoDB item
    - Include auth helpers (`_extract_claims`, `_resolve_role`) and `_resolve_correlation_id`
    - Return 200 with `{"users": [...]}` (empty array if no users)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [x] 4.2 Write unit tests for GET /v1/users handler in `tests/sitespy/test_users_get.py`
    - Test tenant_admin lists users for own tenant (200 with users array)
    - Test super_admin lists users for specified tenant (200 with users array)
    - Test empty tenant returns 200 with empty `users` array
    - Test super_admin without `tenant_id` query param returns 400
    - Test unauthorized role (plain `user`) returns 403
    - Test DynamoDB query failure returns 500
    - Test correlation ID echoed in response header
    - Test CORS headers present on all responses
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 3.1, 3.2, 3.3, 3.7_

  - [x] 4.3 Write property test for role enforcement
    - **Property 1: Role enforcement rejects unauthorized callers**
    - **Validates: Requirements 2.2**

  - [x] 4.4 Write property test for tenant ID resolution by role
    - **Property 2: Tenant ID resolution by role**
    - **Validates: Requirements 2.3, 2.4, 2.5**

  - [x] 4.5 Write property test for query-to-response mapping
    - **Property 5: Query-to-response mapping preserves all fields**
    - **Validates: Requirements 2.6, 2.7**

- [x] 5. Checkpoint - Ensure GET handler and all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Add SAM template resource and wire together
  - [x] 6.1 Add `UsersGetFunction` resource to `template.yaml`
    - Add new `AWS::Serverless::Function` resource with handler `sitespy.handlers.users_get.handler`
    - Set Runtime python3.12, CodeUri src/, MemorySize 256, Timeout 10
    - Set environment variable `DATA_TABLE: !Ref DataTable`
    - Attach `DynamoDBReadPolicy` for DataTable
    - Add API event for GET /v1/users on SiteSpyApi with CognitoAuthorizer
    - _Requirements: 2.1_

- [x] 7. Property tests for common handler behaviour
  - [x] 7.1 Write property test for correlation ID round-trip
    - **Property 6: Correlation ID round-trip**
    - **Validates: Requirements 3.1, 3.2, 3.3**

  - [x] 7.2 Write property test for CORS headers present on all responses
    - **Property 7: CORS headers present on all responses**
    - **Validates: Requirements 3.7**

  - [x] 7.3 Write property test for unhandled exception safety
    - **Property 8: Unhandled exceptions never leak details**
    - **Validates: Requirements 3.6**

- [ ] 8. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- All property tests use Hypothesis with `@settings(max_examples=100)` minimum
- The GET handler follows the exact structural pattern of `cameras_get.py`
- The POST handler modification adds DynamoDB write after existing Cognito logic

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "4.1", "6.1"] },
    { "id": 3, "tasks": ["4.2", "4.3", "4.4", "4.5"] },
    { "id": 4, "tasks": ["7.1", "7.2", "7.3"] }
  ]
}
```
