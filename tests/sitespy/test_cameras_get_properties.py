"""Property-based tests for the cameras_get handler.

Feature: admin-management-endpoints
Property 11: Camera listing never exposes credentials

**Validates: Requirements 5.8, 5.10**
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

from sitespy.data import _dynamodb_client
from sitespy.handlers.cameras_get import handler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TABLE_NAME = "test-data-table"

# Fields that must NEVER appear in the cameras listing response
_CREDENTIAL_FIELDS = {"username", "password", "ingest_credentials", "secret"}


def _create_table(client) -> None:
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


def _seed_site(client, tenant_id: str, site_id: str) -> None:
    """Insert a site record so the handler can verify site existence."""
    client.put_item(
        TableName=_TABLE_NAME,
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"SITE#{site_id}"},
            "site_name": {"S": "Test Site"},
            "latitude": {"N": "51.5074"},
            "longitude": {"N": "-0.1278"},
            "timezone": {"S": "Europe/London"},
        },
    )


def _seed_camera(
    client,
    tenant_id: str,
    site_id: str,
    camera_id: str,
    camera_name: str,
    camera_model: str | None,
) -> None:
    """Insert a camera record directly into DynamoDB.

    Includes credential-like fields to simulate data that might leak.
    """
    item: dict = {
        "PK": {"S": f"TENANT#{tenant_id}"},
        "SK": {"S": f"SITE#{site_id}#CAM#{camera_id}"},
        "camera_name": {"S": camera_name},
        # Simulate credential data that could be accidentally stored or leaked
        "username": {"S": "sitespy_cam_leaked12345678"},
        "password": {"S": "leaked_password_48chars_aaaaaaaaaaaaaaaaaaaaaa"},
        "secret": {"S": "some_secret_value"},
    }
    if camera_model is not None:
        item["camera_model"] = {"S": camera_model}
    client.put_item(TableName=_TABLE_NAME, Item=item)


def _build_event(
    tenant_id: str,
    site_id: str,
    *,
    groups: str = "SuperAdmins",
    tenant_id_claim: str | None = None,
) -> dict:
    """Build an API Gateway proxy event for GET /v1/sites/{site_id}/cameras."""
    claims: dict = {"cognito:groups": groups}
    if tenant_id_claim is not None:
        claims["custom:tenant_id"] = tenant_id_claim

    query_params: dict[str, str] | None = None
    if groups == "SuperAdmins":
        query_params = {"tenant_id": tenant_id}

    return {
        "httpMethod": "GET",
        "path": f"/v1/sites/{site_id}/cameras",
        "headers": {"X-Correlation-Id": "test-corr-id"},
        "queryStringParameters": query_params,
        "pathParameters": {"site_id": site_id},
        "requestContext": {
            "resourcePath": "/v1/sites/{site_id}/cameras",
            "httpMethod": "GET",
            "stage": "prod",
            "authorizer": {"claims": claims},
        },
        "body": None,
        "isBase64Encoded": False,
    }


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

# Valid camera_id values (matching ^[a-z0-9_]{1,64}$)
_VALID_CAMERA_IDS = st.from_regex(r"[a-z0-9_]{1,20}", fullmatch=True)

# Camera names — non-empty printable strings
_VALID_CAMERA_NAMES = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "Z"),
        blacklist_characters=("\x00",),
    ),
    min_size=1,
    max_size=64,
).filter(lambda s: s.strip())

# Camera models — optional, non-empty printable strings or None
_VALID_CAMERA_MODELS = st.one_of(
    st.none(),
    st.text(
        alphabet=st.characters(
            whitelist_categories=("L", "N", "P", "Z"),
            blacklist_characters=("\x00",),
        ),
        min_size=1,
        max_size=64,
    ).filter(lambda s: s.strip()),
)

# Lists of cameras to seed (1 to 5 cameras per test)
_CAMERA_LISTS = st.lists(
    st.tuples(_VALID_CAMERA_IDS, _VALID_CAMERA_NAMES, _VALID_CAMERA_MODELS),
    min_size=1,
    max_size=5,
    unique_by=lambda t: t[0],  # unique camera_ids
)


# ---------------------------------------------------------------------------
# Property 11: Camera listing never exposes credentials
# Validates: Requirements 5.8, 5.10
# ---------------------------------------------------------------------------


@given(cameras=_CAMERA_LISTS)
@settings(max_examples=100, deadline=None)
def test_camera_listing_never_exposes_credentials(
    cameras: list[tuple[str, str, str | None]],
) -> None:
    """Property 11: Camera listing never exposes credentials.

    For any set of cameras registered on a site (with credentials stored
    in Secrets Manager), the GET cameras listing response SHALL NOT contain
    any credential-related fields (username, password, ingest_credentials,
    secret).

    Feature: admin-management-endpoints, Property 11: Camera listing never exposes credentials

    **Validates: Requirements 5.8, 5.10**
    """
    tenant_id = "test_tenant"
    site_id = "test_site"

    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        # Seed the site
        _seed_site(client, tenant_id, site_id)

        # Seed cameras with credential-like fields in DynamoDB
        for camera_id, camera_name, camera_model in cameras:
            _seed_camera(client, tenant_id, site_id, camera_id, camera_name, camera_model)

        # Call the handler as super_admin
        event = _build_event(tenant_id, site_id, groups="SuperAdmins")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])

        assert "cameras" in body
        assert len(body["cameras"]) == len(cameras)

        # Check that no credential fields appear in any camera record
        for camera_record in body["cameras"]:
            record_fields = set(camera_record.keys())
            leaked_fields = record_fields & _CREDENTIAL_FIELDS
            assert not leaked_fields, (
                f"Credential fields leaked in response: {leaked_fields}"
            )

        # Also verify the raw response body string doesn't contain credential values
        raw_body = result["body"]
        assert "sitespy_cam_leaked12345678" not in raw_body
        assert "leaked_password_48chars" not in raw_body
        assert "some_secret_value" not in raw_body

        # Verify only allowed fields are present
        allowed_fields = {"camera_id", "camera_name", "camera_model"}
        for camera_record in body["cameras"]:
            record_fields = set(camera_record.keys())
            unexpected_fields = record_fields - allowed_fields
            assert not unexpected_fields, (
                f"Unexpected fields in camera response: {unexpected_fields}"
            )
