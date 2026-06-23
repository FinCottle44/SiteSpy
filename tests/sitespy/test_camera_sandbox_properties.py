"""Property-based tests for the camera-sandbox feature.

Feature: camera-sandbox
Property 1: Sandbox provisioning idempotence
Property 2: Sandbox visibility exclusion
Property 3: Transfer role enforcement
Property 4: Transfer request body validation
Property 5: Transfer precondition validation
Property 7: Transfer conflict detection
Property 8: Transfer does not move snapshot records
Property 9: Post-transfer token resolution

**Validates: Requirements 1.2, 1.3, 2.1, 2.2, 2.3, 5.1, 5.2, 5.7, 5.8, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 7.2**
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import boto3
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws
from unittest.mock import MagicMock

from sitespy.data import (
    _dynamodb_client,
    ensure_sandbox_default_site,
    ensure_sandbox_tenant_record,
    get_camera,
    get_camera_by_token,
    get_site,
    get_tenant,
    put_camera,
    put_site,
    put_tenant,
    transfer_camera,
)
from sitespy.errors import Forbidden
from sitespy.handlers.cameras_transfer import handler as transfer_handler
from sitespy.sandbox import (
    SANDBOX_DEFAULT_SITE_ID,
    SANDBOX_TENANT_ID,
    ensure_sandbox_tenant,
    sandbox_visibility_guard,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TABLE_NAME = "test-data-table"


def _create_table(client):
    """Create the test DynamoDB table matching the project schema."""
    client.create_table(
        TableName=_TABLE_NAME,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI1",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env_and_cache():
    """Set env vars and clear the _dynamodb_client lru_cache."""
    os.environ.setdefault("DATA_TABLE", _TABLE_NAME)
    os.environ.setdefault("AWS_REGION", "eu-west-2")
    os.environ.setdefault("SNAPSHOTS_BUCKET", "test-bucket")
    _dynamodb_client.cache_clear()
    yield
    _dynamodb_client.cache_clear()


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Pre-state: whether the sandbox tenant and default site already exist before
# calling ensure_sandbox_tenant(). We test all combinations:
# - Neither exists (fresh table)
# - Only tenant exists (partial provision)
# - Both tenant and site exist (already fully provisioned)
_PRE_STATE = st.sampled_from([
    "empty",           # Neither tenant nor site exists
    "tenant_only",     # Tenant exists but not the default site
    "fully_provisioned",  # Both tenant and site already exist
])


# ---------------------------------------------------------------------------
# Property 1: Sandbox provisioning idempotence
# Validates: Requirements 1.2, 1.3
# ---------------------------------------------------------------------------


@given(pre_state=_PRE_STATE)
@settings(max_examples=200)
def test_sandbox_provisioning_idempotence(pre_state: str) -> None:
    """Property 1: Sandbox provisioning idempotence.

    For any initial state of the DynamoDB table (sandbox tenant exists or does
    not exist), calling ensure_sandbox_tenant() should result in the sandbox
    tenant and default site records existing, and calling it again should leave
    those records unchanged.

    Feature: camera-sandbox, Property 1: Sandbox provisioning idempotence

    **Validates: Requirements 1.2, 1.3**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        # Set up the pre-state
        if pre_state == "tenant_only":
            # Only create the tenant record, not the site
            ensure_sandbox_tenant_record()
        elif pre_state == "fully_provisioned":
            # Create both tenant and default site
            ensure_sandbox_tenant_record()
            ensure_sandbox_default_site()

        # --- First call to ensure_sandbox_tenant ---
        ensure_sandbox_tenant()

        # After the first call, both records MUST exist
        tenant_after_first = get_tenant(SANDBOX_TENANT_ID)
        site_after_first = get_site(SANDBOX_TENANT_ID, SANDBOX_DEFAULT_SITE_ID)

        assert tenant_after_first is not None, "Tenant record must exist after ensure_sandbox_tenant()"
        assert site_after_first is not None, "Default site record must exist after ensure_sandbox_tenant()"

        # Verify tenant record has correct attributes
        assert tenant_after_first["tenant_name"]["S"] == "Sandbox Construction"
        assert tenant_after_first["stale_threshold_hours"]["N"] == "24"
        assert "created_at" in tenant_after_first

        # Verify site record has correct attributes
        assert site_after_first["site_name"]["S"] == "Default Sandbox Site"
        assert site_after_first["latitude"]["N"] == str(-33.8688)
        assert site_after_first["longitude"]["N"] == str(151.2093)
        assert site_after_first["timezone"]["S"] == "Australia/Sydney"
        assert "created_at" in site_after_first

        # --- Second call to ensure_sandbox_tenant (idempotence check) ---
        ensure_sandbox_tenant()

        tenant_after_second = get_tenant(SANDBOX_TENANT_ID)
        site_after_second = get_site(SANDBOX_TENANT_ID, SANDBOX_DEFAULT_SITE_ID)

        assert tenant_after_second is not None, "Tenant record must still exist after second call"
        assert site_after_second is not None, "Default site record must still exist after second call"

        # Records should be completely unchanged after the second call
        assert tenant_after_first == tenant_after_second, (
            "Tenant record must be unchanged after second ensure_sandbox_tenant() call"
        )
        assert site_after_first == site_after_second, (
            "Site record must be unchanged after second ensure_sandbox_tenant() call"
        )


# ---------------------------------------------------------------------------
# Strategies for Property 2 and Property 6
# ---------------------------------------------------------------------------

# Non-sandbox tenant IDs — valid identifiers that are NOT the sandbox tenant
_NON_SANDBOX_TENANT_IDS = st.from_regex(
    r"[a-z][a-z0-9_]{2,30}", fullmatch=True
).filter(lambda t: t != SANDBOX_TENANT_ID)

# Random tenant lists that always include sandbox_construction
_TENANT_LISTS_WITH_SANDBOX = st.lists(
    _NON_SANDBOX_TENANT_IDS,
    min_size=0,
    max_size=10,
    unique=True,
).map(lambda tenants: tenants + [SANDBOX_TENANT_ID])

# Roles that should be forbidden from accessing sandbox
_FORBIDDEN_ROLES = st.sampled_from(["tenant_admin", "user"])


# ---------------------------------------------------------------------------
# Property 2: Sandbox visibility exclusion
# Validates: Requirements 2.1, 2.2, 2.3
# ---------------------------------------------------------------------------


@given(tenant_list=_TENANT_LISTS_WITH_SANDBOX, role=_FORBIDDEN_ROLES)
@settings(max_examples=200)
def test_sandbox_visibility_guard_raises_forbidden_for_non_super_admin(
    tenant_list: list[str],
    role: str,
) -> None:
    """Property 2: Sandbox visibility exclusion (forbidden roles).

    For any set of tenant records in the system that includes
    sandbox_construction, and for any caller with role tenant_admin or user,
    direct access to the sandbox tenant shall raise Forbidden.

    Feature: camera-sandbox, Property 2: Sandbox visibility exclusion

    **Validates: Requirements 2.1, 2.2, 2.3**
    """
    # The sandbox tenant is in the list — verify the guard raises Forbidden
    assert SANDBOX_TENANT_ID in tenant_list

    with pytest.raises(Forbidden):
        sandbox_visibility_guard(SANDBOX_TENANT_ID, role)

    # Non-sandbox tenants in the same list should NOT raise Forbidden
    for tenant_id in tenant_list:
        if tenant_id != SANDBOX_TENANT_ID:
            # Should not raise for any role on non-sandbox tenants
            sandbox_visibility_guard(tenant_id, role)


@given(tenant_list=_TENANT_LISTS_WITH_SANDBOX)
@settings(max_examples=200)
def test_sandbox_visibility_guard_allows_super_admin(
    tenant_list: list[str],
) -> None:
    """Property 2: Sandbox visibility exclusion (super_admin allowed).

    For any set of tenant records in the system that includes
    sandbox_construction, a caller with role super_admin shall NOT be
    blocked from accessing the sandbox tenant.

    Feature: camera-sandbox, Property 2: Sandbox visibility exclusion

    **Validates: Requirements 2.2, 2.3**
    """
    assert SANDBOX_TENANT_ID in tenant_list

    # super_admin should never raise Forbidden for the sandbox tenant
    sandbox_visibility_guard(SANDBOX_TENANT_ID, "super_admin")

    # super_admin should also not raise for any other tenant
    for tenant_id in tenant_list:
        sandbox_visibility_guard(tenant_id, "super_admin")


# ---------------------------------------------------------------------------
# Strategies for Property 3
# ---------------------------------------------------------------------------

# Non-super_admin roles
_NON_SUPER_ADMIN_ROLES = st.sampled_from(["tenant_admin", "user"])

# Map roles to their Cognito group names
_ROLE_TO_GROUP = {
    "tenant_admin": "TenantAdmins",
    "user": "Users",
}

# Random request body content — the body content should not matter for role enforcement
_RANDOM_BODY_CONTENT = st.fixed_dictionaries(
    {},
    optional={
        "source_tenant_id": st.text(min_size=0, max_size=50),
        "source_site_id": st.text(min_size=0, max_size=50),
        "camera_id": st.text(min_size=0, max_size=50),
        "target_tenant_id": st.text(min_size=0, max_size=50),
        "target_site_id": st.text(min_size=0, max_size=50),
        "extra_field": st.text(min_size=0, max_size=20),
    },
)


def _make_apigw_event(role: str, body: dict) -> dict:
    """Build an API Gateway proxy event with JWT claims for the given role."""
    group = _ROLE_TO_GROUP.get(role, "Users")
    return {
        "httpMethod": "POST",
        "path": "/v1/cameras/transfer",
        "headers": {
            "Content-Type": "application/json",
            "X-Correlation-Id": "test-correlation-id",
        },
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:groups": group,
                    "sub": "test-user-id",
                }
            }
        },
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# Property 3: Transfer role enforcement
# Validates: Requirements 6.2, 6.3
# ---------------------------------------------------------------------------


@given(role=_NON_SUPER_ADMIN_ROLES, body=_RANDOM_BODY_CONTENT)
@settings(max_examples=200)
def test_transfer_role_enforcement(role: str, body: dict) -> None:
    """Property 3: Transfer role enforcement.

    For any caller whose role is not super_admin, the POST /v1/cameras/transfer
    endpoint shall return 403 Forbidden regardless of the request body content.

    Feature: camera-sandbox, Property 3: Transfer role enforcement

    **Validates: Requirements 6.2, 6.3**
    """
    event = _make_apigw_event(role, body)
    response = transfer_handler(event, MagicMock())

    assert response["statusCode"] == 403, (
        f"Expected 403 for role={role}, got {response['statusCode']}"
    )

    response_body = json.loads(response["body"])
    assert response_body["error"] == "ACCESS_DENIED", (
        f"Expected ACCESS_DENIED error key, got {response_body.get('error')}"
    )


# ---------------------------------------------------------------------------
# Strategies for Property 4
# ---------------------------------------------------------------------------

# The 5 required fields for the transfer request body
_TRANSFER_REQUIRED_FIELDS = [
    "source_tenant_id",
    "source_site_id",
    "camera_id",
    "target_tenant_id",
    "target_site_id",
]

# Valid non-empty field value
_VALID_FIELD_VALUE = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P")),
    min_size=1,
    max_size=30,
).filter(lambda s: s.strip() != "")

# Strategy to produce a "bad" value: either missing (None) or empty string
_BAD_FIELD_VALUE = st.sampled_from([None, ""])


@st.composite
def _invalid_transfer_body(draw):
    """Generate a request body where at least one required field is missing or empty.

    Each field is independently either valid or bad, with the constraint that at
    least one field must be bad (missing or empty string).
    """
    # For each field, decide if it's valid or bad
    field_states = draw(
        st.lists(
            st.booleans(),  # True = valid, False = bad
            min_size=5,
            max_size=5,
        ).filter(lambda states: not all(states))  # At least one must be bad
    )

    body = {}
    invalid_fields = []

    for field_name, is_valid in zip(_TRANSFER_REQUIRED_FIELDS, field_states):
        if is_valid:
            body[field_name] = draw(_VALID_FIELD_VALUE)
        else:
            bad_value = draw(_BAD_FIELD_VALUE)
            if bad_value is not None:
                body[field_name] = bad_value  # empty string
            # else: field is missing from body entirely
            invalid_fields.append(field_name)

    return body, invalid_fields


# ---------------------------------------------------------------------------
# Property 4: Transfer request body validation
# Validates: Requirements 6.1, 6.4
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(max_examples=200)
def test_transfer_request_body_validation(data) -> None:
    """Property 4: Transfer request body validation.

    For any request body that is missing one or more of the required fields
    (source_tenant_id, source_site_id, camera_id, target_tenant_id,
    target_site_id) or contains empty string values for any of them, the
    transfer endpoint shall return 400 Bad Request.

    Feature: camera-sandbox, Property 4: Transfer request body validation

    **Validates: Requirements 6.1, 6.4**
    """
    body, invalid_fields = data.draw(_invalid_transfer_body())

    # Use super_admin role so the role check passes — we want to test body validation
    event = _make_super_admin_event(body)
    response = transfer_handler(event, MagicMock())

    assert response["statusCode"] == 400, (
        f"Expected 400 for body with invalid fields {invalid_fields}, "
        f"got {response['statusCode']}. Body sent: {body}"
    )

    response_body = json.loads(response["body"])
    assert response_body["error"] == "BAD_REQUEST", (
        f"Expected BAD_REQUEST error key, got {response_body.get('error')}"
    )

    # The response message should indicate which field is invalid.
    # The handler validates fields sequentially, so it reports the first invalid field.
    message = response_body.get("message", "")
    # At least one of the invalid fields should be mentioned in the message
    field_mentioned = any(field in message for field in invalid_fields)
    assert field_mentioned, (
        f"Expected response message to mention one of {invalid_fields}, "
        f"got message: {message}"
    )


# ---------------------------------------------------------------------------
# Strategies for Property 5
# ---------------------------------------------------------------------------

# Valid identifier strategy — sampled from a pool for speed with mock_aws
_VALID_ID = st.sampled_from([
    "alpha", "bravo", "charlie", "delta", "echo",
    "foxtrot", "golf", "hotel", "india", "juliet",
    "kilo", "lima", "mike", "november", "oscar",
])

# Precondition failure scenarios:
# - "source_camera_missing": source camera doesn't exist
# - "target_tenant_missing": target tenant doesn't exist
# - "target_site_missing": target site doesn't belong to target tenant
_PRECONDITION_FAILURE_SCENARIO = st.sampled_from([
    "source_camera_missing",
    "target_tenant_missing",
    "target_site_missing",
])


def _make_super_admin_event(body: dict) -> dict:
    """Build an API Gateway proxy event with super_admin role."""
    return {
        "httpMethod": "POST",
        "path": "/v1/cameras/transfer",
        "headers": {
            "Content-Type": "application/json",
            "X-Correlation-Id": "test-correlation-id",
        },
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:groups": "SuperAdmins",
                    "sub": "test-super-admin",
                }
            }
        },
        "body": json.dumps(body),
    }


def _seed_camera(client, tenant_id: str, site_id: str, camera_id: str) -> None:
    """Seed a camera record directly in DynamoDB."""
    table_name = "test-data-table"
    client.put_item(
        TableName=table_name,
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"SITE#{site_id}#CAM#{camera_id}"},
            "GSI1PK": {"S": f"TOKEN#tok_{camera_id}"},
            "GSI1SK": {"S": "CAMERA"},
            "camera_name": {"S": f"Camera {camera_id}"},
            "ingest_token": {"S": f"tok_{camera_id}"},
            "created_at": {"S": "2024-01-01T00:00:00Z"},
        },
    )


def _seed_tenant(client, tenant_id: str) -> None:
    """Seed a tenant record directly in DynamoDB."""
    table_name = "test-data-table"
    client.put_item(
        TableName=table_name,
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"TENANT#{tenant_id}"},
            "tenant_name": {"S": f"Tenant {tenant_id}"},
            "stale_threshold_hours": {"N": "24"},
            "created_at": {"S": "2024-01-01T00:00:00Z"},
        },
    )


def _seed_site(client, tenant_id: str, site_id: str) -> None:
    """Seed a site record directly in DynamoDB."""
    table_name = "test-data-table"
    client.put_item(
        TableName=table_name,
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"SITE#{site_id}"},
            "site_name": {"S": f"Site {site_id}"},
            "latitude": {"N": "-33.8688"},
            "longitude": {"N": "151.2093"},
            "timezone": {"S": "Australia/Sydney"},
            "created_at": {"S": "2024-01-01T00:00:00Z"},
        },
    )


def _scan_all_items(client) -> list[dict]:
    """Scan all items in the test table."""
    table_name = "test-data-table"
    items = []
    kwargs: dict = {"TableName": table_name}
    while True:
        response = client.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return items


# ---------------------------------------------------------------------------
# Property 5: Transfer precondition validation
# Validates: Requirements 5.1, 5.2, 6.5, 6.6
# ---------------------------------------------------------------------------


@given(
    scenario=_PRECONDITION_FAILURE_SCENARIO,
    source_tenant=_VALID_ID,
    source_site=_VALID_ID,
    camera_id=_VALID_ID,
    target_tenant=_VALID_ID,
    target_site=_VALID_ID,
)
@settings(max_examples=200)
def test_transfer_precondition_validation(
    scenario: str,
    source_tenant: str,
    source_site: str,
    camera_id: str,
    target_tenant: str,
    target_site: str,
) -> None:
    """Property 5: Transfer precondition validation.

    For any transfer request where the source camera does not exist, or the
    target tenant does not exist, or the target site does not exist within the
    target tenant, the transfer endpoint shall return an appropriate error (404)
    and leave all existing records unchanged.

    Feature: camera-sandbox, Property 5: Transfer precondition validation

    **Validates: Requirements 5.1, 5.2, 6.5, 6.6**
    """
    # Ensure source and target identifiers are distinct to avoid accidental collisions
    # that could confuse scenarios (e.g., source_tenant == target_tenant)
    target_tenant = target_tenant + "_tgt" if target_tenant == source_tenant else target_tenant
    target_site = target_site + "_tgt" if target_site == source_site else target_site

    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        # --- Set up scenario-specific DynamoDB state ---
        if scenario == "source_camera_missing":
            # Source camera does NOT exist, but target tenant and site DO
            _seed_tenant(client, source_tenant)
            _seed_site(client, source_tenant, source_site)
            _seed_tenant(client, target_tenant)
            _seed_site(client, target_tenant, target_site)
            # No camera seeded at source

        elif scenario == "target_tenant_missing":
            # Source camera EXISTS, but target tenant does NOT exist
            _seed_tenant(client, source_tenant)
            _seed_site(client, source_tenant, source_site)
            _seed_camera(client, source_tenant, source_site, camera_id)
            # No target tenant/site seeded

        elif scenario == "target_site_missing":
            # Source camera EXISTS, target tenant EXISTS, but target site
            # does NOT belong to target tenant
            _seed_tenant(client, source_tenant)
            _seed_site(client, source_tenant, source_site)
            _seed_camera(client, source_tenant, source_site, camera_id)
            _seed_tenant(client, target_tenant)
            # Target site not seeded under target tenant

        # Capture all records before the transfer attempt
        items_before = _scan_all_items(client)

        # --- Make the transfer request ---
        body = {
            "source_tenant_id": source_tenant,
            "source_site_id": source_site,
            "camera_id": camera_id,
            "target_tenant_id": target_tenant,
            "target_site_id": target_site,
        }
        event = _make_super_admin_event(body)
        response = transfer_handler(event, MagicMock())

        # --- Assert 404 is returned ---
        assert response["statusCode"] == 404, (
            f"Expected 404 for scenario={scenario}, got {response['statusCode']}. "
            f"Body: {response.get('body')}"
        )

        response_body = json.loads(response["body"])
        assert response_body["error"] == "NOT_FOUND", (
            f"Expected NOT_FOUND error key, got {response_body.get('error')}"
        )

        # --- Verify no records were created or modified ---
        items_after = _scan_all_items(client)

        # Sort for stable comparison
        sort_key = lambda item: (item["PK"]["S"], item["SK"]["S"])
        assert sorted(items_before, key=sort_key) == sorted(items_after, key=sort_key), (
            f"Records were modified during failed transfer (scenario={scenario}). "
            f"Before: {len(items_before)} items, After: {len(items_after)} items"
        )


# ---------------------------------------------------------------------------
# Strategies for Property 7
# ---------------------------------------------------------------------------

# Camera attributes for conflict testing
_CAMERA_NAME = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "Pd")),
    min_size=1,
    max_size=30,
).filter(lambda s: s.strip() != "")

_CAMERA_MODEL = st.one_of(
    st.none(),
    st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "Pd")),
        min_size=1,
        max_size=30,
    ).filter(lambda s: s.strip() != ""),
)

_INGEST_TOKEN = st.from_regex(r"[a-z0-9]{8,32}", fullmatch=True)


# ---------------------------------------------------------------------------
# Property 7: Transfer conflict detection
# Validates: Requirements 5.7, 6.7
# ---------------------------------------------------------------------------


@given(
    source_tenant=_VALID_ID,
    source_site=_VALID_ID,
    camera_id=_VALID_ID,
    target_tenant=_VALID_ID,
    target_site=_VALID_ID,
    camera_name=_CAMERA_NAME,
    camera_model=_CAMERA_MODEL,
    ingest_token=_INGEST_TOKEN,
)
@settings(max_examples=200, deadline=None)
def test_transfer_conflict_detection(
    source_tenant: str,
    source_site: str,
    camera_id: str,
    target_tenant: str,
    target_site: str,
    camera_name: str,
    camera_model: str | None,
    ingest_token: str,
) -> None:
    """Property 7: Transfer conflict detection.

    For any transfer request where a camera with the same camera_id already
    exists at the target site, the transfer endpoint shall return 409 Conflict
    and leave the source camera unchanged.

    Feature: camera-sandbox, Property 7: Transfer conflict detection

    **Validates: Requirements 5.7, 6.7**
    """
    # Ensure source and target are distinct to avoid confusion
    target_tenant = target_tenant + "_tgt" if target_tenant == source_tenant else target_tenant
    target_site = target_site + "_tgt" if target_site == source_site else target_site

    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        # --- Seed source tenant, site, and camera ---
        _seed_tenant(client, source_tenant)
        _seed_site(client, source_tenant, source_site)

        # Seed source camera with generated attributes
        source_camera_item = {
            "PK": {"S": f"TENANT#{source_tenant}"},
            "SK": {"S": f"SITE#{source_site}#CAM#{camera_id}"},
            "GSI1PK": {"S": f"TOKEN#{ingest_token}"},
            "GSI1SK": {"S": "CAMERA"},
            "camera_name": {"S": camera_name},
            "ingest_token": {"S": ingest_token},
            "created_at": {"S": "2024-01-01T00:00:00Z"},
        }
        if camera_model is not None:
            source_camera_item["camera_model"] = {"S": camera_model}

        client.put_item(TableName=_TABLE_NAME, Item=source_camera_item)

        # --- Seed target tenant and site ---
        _seed_tenant(client, target_tenant)
        _seed_site(client, target_tenant, target_site)

        # --- Seed conflicting camera at target with same camera_id ---
        conflicting_camera_item = {
            "PK": {"S": f"TENANT#{target_tenant}"},
            "SK": {"S": f"SITE#{target_site}#CAM#{camera_id}"},
            "GSI1PK": {"S": f"TOKEN#conflict_tok_{camera_id}"},
            "GSI1SK": {"S": "CAMERA"},
            "camera_name": {"S": "Conflicting Camera"},
            "ingest_token": {"S": f"conflict_tok_{camera_id}"},
            "created_at": {"S": "2024-02-01T00:00:00Z"},
        }
        client.put_item(TableName=_TABLE_NAME, Item=conflicting_camera_item)

        # --- Make the transfer request ---
        body = {
            "source_tenant_id": source_tenant,
            "source_site_id": source_site,
            "camera_id": camera_id,
            "target_tenant_id": target_tenant,
            "target_site_id": target_site,
        }
        event = _make_super_admin_event(body)
        response = transfer_handler(event, MagicMock())

        # --- Assert 409 Conflict is returned ---
        assert response["statusCode"] == 409, (
            f"Expected 409 for conflict at target, got {response['statusCode']}. "
            f"Body: {response.get('body')}"
        )

        response_body = json.loads(response["body"])
        assert response_body["error"] == "CONFLICT", (
            f"Expected CONFLICT error key, got {response_body.get('error')}"
        )

        # --- Verify source camera remains unchanged ---
        source_camera_after = client.get_item(
            TableName=_TABLE_NAME,
            Key={
                "PK": {"S": f"TENANT#{source_tenant}"},
                "SK": {"S": f"SITE#{source_site}#CAM#{camera_id}"},
            },
        ).get("Item")

        assert source_camera_after is not None, (
            "Source camera must still exist after conflict rejection"
        )
        assert source_camera_after["camera_name"]["S"] == camera_name, (
            "Source camera name must be unchanged after conflict rejection"
        )
        assert source_camera_after["ingest_token"]["S"] == ingest_token, (
            "Source camera ingest_token must be unchanged after conflict rejection"
        )
        if camera_model is not None:
            assert source_camera_after.get("camera_model", {}).get("S") == camera_model, (
                "Source camera model must be unchanged after conflict rejection"
            )


# ---------------------------------------------------------------------------
# Strategies for Property 6
# ---------------------------------------------------------------------------

# Camera attribute strategies — constrained to realistic non-empty strings
_CAMERA_ID = st.from_regex(r"[a-z][a-z0-9_]{2,20}", fullmatch=True)
_CAMERA_NAME = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
    min_size=1,
    max_size=50,
).filter(lambda s: s.strip() != "")
_CAMERA_MODEL = st.one_of(
    st.none(),
    st.text(
        alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
        min_size=1,
        max_size=30,
    ).filter(lambda s: s.strip() != ""),
)
_INGEST_TOKEN = st.from_regex(r"tok_[a-z0-9]{8,20}", fullmatch=True)

# Tenant/site IDs for transfer — use distinct pools to avoid collisions
_SOURCE_TENANT_ID = st.sampled_from(["src_tenant_a", "src_tenant_b", "src_tenant_c"])
_SOURCE_SITE_ID = st.sampled_from(["src_site_x", "src_site_y", "src_site_z"])
_TARGET_TENANT_ID = st.sampled_from(["tgt_tenant_a", "tgt_tenant_b", "tgt_tenant_c"])
_TARGET_SITE_ID = st.sampled_from(["tgt_site_x", "tgt_site_y", "tgt_site_z"])


# ---------------------------------------------------------------------------
# Property 6: Transfer preserves camera attributes atomically
# Validates: Requirements 5.3, 5.4, 5.6, 6.8
# ---------------------------------------------------------------------------


@given(
    camera_id=_CAMERA_ID,
    camera_name=_CAMERA_NAME,
    camera_model=_CAMERA_MODEL,
    ingest_token=_INGEST_TOKEN,
    source_tenant=_SOURCE_TENANT_ID,
    source_site=_SOURCE_SITE_ID,
    target_tenant=_TARGET_TENANT_ID,
    target_site=_TARGET_SITE_ID,
)
@settings(max_examples=200)
def test_transfer_preserves_camera_attributes_atomically(
    camera_id: str,
    camera_name: str,
    camera_model: str | None,
    ingest_token: str,
    source_tenant: str,
    source_site: str,
    target_tenant: str,
    target_site: str,
) -> None:
    """Property 6: Transfer preserves camera attributes atomically.

    For any valid camera record with arbitrary camera_id, camera_name,
    camera_model, and ingest_token, after a successful transfer the target
    record shall contain identical values for all of these attributes, the
    source record shall no longer exist, and the response shall contain the
    camera's new tenant_id, site_id, and camera_id.

    Feature: camera-sandbox, Property 6: Transfer preserves camera attributes atomically

    **Validates: Requirements 5.3, 5.4, 5.6, 6.8**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        # --- Seed source camera with random attributes ---
        source_item: dict = {
            "PK": {"S": f"TENANT#{source_tenant}"},
            "SK": {"S": f"SITE#{source_site}#CAM#{camera_id}"},
            "GSI1PK": {"S": f"TOKEN#{ingest_token}"},
            "GSI1SK": {"S": "CAMERA"},
            "camera_name": {"S": camera_name},
            "ingest_token": {"S": ingest_token},
            "created_at": {"S": "2024-01-15T10:30:00Z"},
        }
        if camera_model is not None:
            source_item["camera_model"] = {"S": camera_model}

        client.put_item(TableName=_TABLE_NAME, Item=source_item)

        # --- Seed source tenant record (required for get_camera lookup) ---
        _seed_tenant(client, source_tenant)
        _seed_site(client, source_tenant, source_site)

        # --- Seed target tenant and site ---
        _seed_tenant(client, target_tenant)
        _seed_site(client, target_tenant, target_site)

        # --- Call the transfer handler as super_admin ---
        body = {
            "source_tenant_id": source_tenant,
            "source_site_id": source_site,
            "camera_id": camera_id,
            "target_tenant_id": target_tenant,
            "target_site_id": target_site,
        }
        event = _make_super_admin_event(body)
        response = transfer_handler(event, MagicMock())

        # --- Verify response is 200 with correct body ---
        assert response["statusCode"] == 200, (
            f"Expected 200, got {response['statusCode']}. "
            f"Body: {response.get('body')}"
        )

        response_body = json.loads(response["body"])
        assert response_body["tenant_id"] == target_tenant, (
            f"Expected tenant_id={target_tenant}, got {response_body.get('tenant_id')}"
        )
        assert response_body["site_id"] == target_site, (
            f"Expected site_id={target_site}, got {response_body.get('site_id')}"
        )
        assert response_body["camera_id"] == camera_id, (
            f"Expected camera_id={camera_id}, got {response_body.get('camera_id')}"
        )

        # --- Verify target record has identical camera attributes ---
        target_record = client.get_item(
            TableName=_TABLE_NAME,
            Key={
                "PK": {"S": f"TENANT#{target_tenant}"},
                "SK": {"S": f"SITE#{target_site}#CAM#{camera_id}"},
            },
        ).get("Item")

        assert target_record is not None, "Target camera record must exist after transfer"

        # camera_name must match
        assert target_record["camera_name"]["S"] == camera_name, (
            f"Expected camera_name={camera_name!r}, "
            f"got {target_record['camera_name']['S']!r}"
        )

        # camera_model must match (present or absent)
        if camera_model is not None:
            assert "camera_model" in target_record, (
                "camera_model should be present in target record"
            )
            assert target_record["camera_model"]["S"] == camera_model, (
                f"Expected camera_model={camera_model!r}, "
                f"got {target_record['camera_model']['S']!r}"
            )
        else:
            assert "camera_model" not in target_record, (
                "camera_model should NOT be present when source had None"
            )

        # ingest_token must be preserved
        assert target_record["ingest_token"]["S"] == ingest_token, (
            f"Expected ingest_token={ingest_token!r}, "
            f"got {target_record['ingest_token']['S']!r}"
        )

        # GSI1PK must map token correctly
        assert target_record["GSI1PK"]["S"] == f"TOKEN#{ingest_token}", (
            f"Expected GSI1PK=TOKEN#{ingest_token}, "
            f"got {target_record['GSI1PK']['S']}"
        )
        assert target_record["GSI1SK"]["S"] == "CAMERA", (
            f"Expected GSI1SK=CAMERA, got {target_record['GSI1SK']['S']}"
        )

        # transferred_at should be set
        assert "transferred_at" in target_record, (
            "Target record should have a transferred_at timestamp"
        )

        # --- Verify source record no longer exists ---
        source_after = client.get_item(
            TableName=_TABLE_NAME,
            Key={
                "PK": {"S": f"TENANT#{source_tenant}"},
                "SK": {"S": f"SITE#{source_site}#CAM#{camera_id}"},
            },
        ).get("Item")

        assert source_after is None, (
            "Source camera record must NOT exist after successful transfer"
        )



# ---------------------------------------------------------------------------
# Strategies for Property 9
# ---------------------------------------------------------------------------

# Ingest token strategy — alphanumeric tokens of varying lengths
_INGEST_TOKEN = st.from_regex(r"[a-zA-Z0-9]{8,32}", fullmatch=True)

# Camera name strategy
_CAMERA_NAME = st.from_regex(r"[A-Za-z ]{3,30}", fullmatch=True)

# Optional camera model (can be None or a string)
_CAMERA_MODEL = st.one_of(st.none(), st.from_regex(r"[A-Za-z0-9 ]{3,20}", fullmatch=True))


# ---------------------------------------------------------------------------
# Property 9: Post-transfer token resolution
# Validates: Requirements 7.2
# ---------------------------------------------------------------------------


@given(
    source_tenant=_VALID_ID,
    source_site=_VALID_ID,
    camera_id=_VALID_ID,
    target_tenant=_VALID_ID,
    target_site=_VALID_ID,
    ingest_token=_INGEST_TOKEN,
    camera_name=_CAMERA_NAME,
    camera_model=_CAMERA_MODEL,
)
@settings(max_examples=200)
def test_post_transfer_token_resolution(
    source_tenant: str,
    source_site: str,
    camera_id: str,
    target_tenant: str,
    target_site: str,
    ingest_token: str,
    camera_name: str,
    camera_model: str | None,
) -> None:
    """Property 9: Post-transfer token resolution.

    For any successfully transferred camera, querying GSI1 with
    GSI1PK = TOKEN#<ingest_token> shall resolve to the target tenant's camera
    record (with PK = TENANT#<target_tenant_id> and SK containing the target
    site_id and camera_id).

    Feature: camera-sandbox, Property 9: Post-transfer token resolution

    **Validates: Requirements 7.2**
    """
    # Ensure source and target identifiers are distinct to avoid collisions
    target_tenant = target_tenant + "_tgt" if target_tenant == source_tenant else target_tenant
    target_site = target_site + "_tgt" if target_site == source_site else target_site

    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        # --- Seed source camera with the generated ingest_token ---
        client.put_item(
            TableName=_TABLE_NAME,
            Item={
                "PK": {"S": f"TENANT#{source_tenant}"},
                "SK": {"S": f"SITE#{source_site}#CAM#{camera_id}"},
                "GSI1PK": {"S": f"TOKEN#{ingest_token}"},
                "GSI1SK": {"S": "CAMERA"},
                "camera_name": {"S": camera_name},
                "ingest_token": {"S": ingest_token},
                "created_at": {"S": "2024-01-01T00:00:00Z"},
                **({"camera_model": {"S": camera_model}} if camera_model else {}),
            },
        )

        # --- Seed target tenant and site ---
        _seed_tenant(client, target_tenant)
        _seed_site(client, target_tenant, target_site)

        # --- Perform the transfer via the handler (as super_admin) ---
        body = {
            "source_tenant_id": source_tenant,
            "source_site_id": source_site,
            "camera_id": camera_id,
            "target_tenant_id": target_tenant,
            "target_site_id": target_site,
        }
        event = _make_super_admin_event(body)
        response = transfer_handler(event, MagicMock())

        # Confirm transfer succeeded
        assert response["statusCode"] == 200, (
            f"Expected 200 for transfer, got {response['statusCode']}. "
            f"Body: {response.get('body')}"
        )

        # --- Query GSI1 with TOKEN#<ingest_token> ---
        resolved = get_camera_by_token(ingest_token)

        # The token must resolve to a camera record
        assert resolved is not None, (
            f"GSI1 query for TOKEN#{ingest_token} returned None after transfer"
        )

        # Verify the resolved record belongs to the target tenant
        resolved_pk = resolved["PK"]["S"]
        assert resolved_pk == f"TENANT#{target_tenant}", (
            f"Expected PK = TENANT#{target_tenant}, got {resolved_pk}"
        )

        # Verify the SK contains the target site_id and camera_id
        resolved_sk = resolved["SK"]["S"]
        assert f"SITE#{target_site}#CAM#{camera_id}" == resolved_sk, (
            f"Expected SK = SITE#{target_site}#CAM#{camera_id}, got {resolved_sk}"
        )

        # Verify the ingest_token attribute matches
        assert resolved["ingest_token"]["S"] == ingest_token, (
            f"Expected ingest_token = {ingest_token}, "
            f"got {resolved['ingest_token']['S']}"
        )


# ---------------------------------------------------------------------------
# Strategies for Property 8
# ---------------------------------------------------------------------------

# Number of snapshot (IMG#) records to seed for the camera
_NUM_SNAPSHOTS = st.integers(min_value=1, max_value=5)

# Timestamp strategy for snapshot records
_SNAPSHOT_TIMESTAMP = st.from_regex(
    r"2024-0[1-9]-[012][0-9]T[01][0-9]:[0-5][0-9]:[0-5][0-9]Z",
    fullmatch=True,
)


def _seed_snapshot(client, tenant_id: str, site_id: str, camera_id: str, timestamp: str) -> None:
    """Seed a snapshot (IMG#) record directly in DynamoDB."""
    client.put_item(
        TableName=_TABLE_NAME,
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"IMG#{site_id}#{camera_id}#{timestamp}"},
            "s3_key": {"S": f"snapshots/{tenant_id}/{site_id}/{camera_id}/{timestamp}.jpg"},
            "sha256": {"S": "abc123def456"},
            "size_bytes": {"N": "102400"},
            "ingested_at": {"S": timestamp},
        },
    )


# ---------------------------------------------------------------------------
# Property 8: Transfer does not move snapshot records
# Validates: Requirements 5.8
# ---------------------------------------------------------------------------


@given(
    source_site=_VALID_ID,
    camera_id=_VALID_ID,
    target_tenant=_VALID_ID,
    target_site=_VALID_ID,
    camera_name=_CAMERA_NAME,
    camera_model=_CAMERA_MODEL,
    ingest_token=_INGEST_TOKEN,
    num_snapshots=_NUM_SNAPSHOTS,
    timestamps=st.lists(
        _SNAPSHOT_TIMESTAMP, min_size=5, max_size=5, unique=True,
    ),
)
@settings(max_examples=200)
def test_transfer_does_not_move_snapshots(
    source_site: str,
    camera_id: str,
    target_tenant: str,
    target_site: str,
    camera_name: str,
    camera_model: str | None,
    ingest_token: str,
    num_snapshots: int,
    timestamps: list[str],
) -> None:
    """Property 8: Transfer does not move snapshot records.

    For any camera transfer where IMG# records exist under the source tenant
    for the transferred camera, those IMG# records shall remain under the
    source tenant PK after transfer.

    Feature: camera-sandbox, Property 8: Transfer does not move snapshot records

    **Validates: Requirements 5.8**
    """
    # Use the sandbox tenant as the source (the typical transfer scenario)
    source_tenant = SANDBOX_TENANT_ID

    # Ensure target is distinct from source
    target_tenant = target_tenant + "_tgt" if target_tenant == source_tenant else target_tenant
    target_site = target_site + "_tgt" if target_site == source_site else target_site

    # Only use as many timestamps as num_snapshots
    snapshot_timestamps = timestamps[:num_snapshots]

    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        # --- Seed source (sandbox) tenant and site ---
        _seed_tenant(client, source_tenant)
        _seed_site(client, source_tenant, source_site)

        # --- Seed source camera with generated attributes ---
        source_camera_item = {
            "PK": {"S": f"TENANT#{source_tenant}"},
            "SK": {"S": f"SITE#{source_site}#CAM#{camera_id}"},
            "GSI1PK": {"S": f"TOKEN#{ingest_token}"},
            "GSI1SK": {"S": "CAMERA"},
            "camera_name": {"S": camera_name},
            "ingest_token": {"S": ingest_token},
            "created_at": {"S": "2024-01-01T00:00:00Z"},
        }
        if camera_model is not None:
            source_camera_item["camera_model"] = {"S": camera_model}

        client.put_item(TableName=_TABLE_NAME, Item=source_camera_item)

        # --- Seed IMG# records under sandbox tenant ---
        for ts in snapshot_timestamps:
            _seed_snapshot(client, source_tenant, source_site, camera_id, ts)

        # --- Seed target tenant and site ---
        _seed_tenant(client, target_tenant)
        _seed_site(client, target_tenant, target_site)

        # --- Perform the transfer ---
        body = {
            "source_tenant_id": source_tenant,
            "source_site_id": source_site,
            "camera_id": camera_id,
            "target_tenant_id": target_tenant,
            "target_site_id": target_site,
        }
        event = _make_super_admin_event(body)
        response = transfer_handler(event, MagicMock())

        # --- Assert transfer succeeds ---
        assert response["statusCode"] == 200, (
            f"Expected 200 for successful transfer, got {response['statusCode']}. "
            f"Body: {response.get('body')}"
        )

        response_body = json.loads(response["body"])
        assert response_body["tenant_id"] == target_tenant
        assert response_body["site_id"] == target_site
        assert response_body["camera_id"] == camera_id

        # --- Verify all IMG# records still have PK = TENANT#sandbox_construction ---
        sandbox_pk = f"TENANT#{source_tenant}"

        for ts in snapshot_timestamps:
            expected_sk = f"IMG#{source_site}#{camera_id}#{ts}"

            item = client.get_item(
                TableName=_TABLE_NAME,
                Key={
                    "PK": {"S": sandbox_pk},
                    "SK": {"S": expected_sk},
                },
            ).get("Item")

            assert item is not None, (
                f"IMG# record with SK={expected_sk} must remain under "
                f"PK={sandbox_pk} after transfer"
            )
            assert item["PK"]["S"] == sandbox_pk, (
                f"IMG# record PK must still be {sandbox_pk}, got {item['PK']['S']}"
            )

        # --- Verify NO IMG# records were created under the target tenant ---
        target_pk = f"TENANT#{target_tenant}"
        all_items = _scan_all_items(client)

        target_img_records = [
            item for item in all_items
            if item["PK"]["S"] == target_pk
            and item["SK"]["S"].startswith("IMG#")
        ]

        assert len(target_img_records) == 0, (
            f"Expected no IMG# records under target tenant {target_pk}, "
            f"but found {len(target_img_records)}: "
            f"{[item['SK']['S'] for item in target_img_records]}"
        )
