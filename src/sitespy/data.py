"""DynamoDB helpers for SiteSpy — key builders and table operations.

Requirements validated: 1.1, 1.9, 2.3, 2.12, 2.13, 3.11, 3.12, 3.14, 5.6, 6.1, 6.2, 7.2, 7.5, 7.6, 8.1, 8.2, 8.3, 8.4, 8.5,
                       live-view: 2.1, 2.4, 2.5, 2.6, 2.11, 2.13, 4.1, 6.2, 6.4, 6.5
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import boto3
import botocore.config

from sitespy.config import get_settings

logger = logging.getLogger(__name__)

_BOTO_CONFIG = botocore.config.Config(retries={"mode": "standard", "max_attempts": 3})
_RETENTION_YEARS_DEFAULT = 5


@lru_cache(maxsize=1)
def _dynamodb_client() -> Any:
    return boto3.client(
        "dynamodb",
        region_name=get_settings().aws_region,
        config=_BOTO_CONFIG,
    )


# ---------------------------------------------------------------------------
# Pure key builders
# ---------------------------------------------------------------------------


def build_tenant_pk(tenant_id: str) -> str:
    """Build the DynamoDB partition key for a tenant item."""
    return f"TENANT#{tenant_id}"


def build_tenant_sk(tenant_id: str) -> str:
    """Build the DynamoDB sort key for a tenant item."""
    return f"TENANT#{tenant_id}"


def build_camera_sk(site_id: str, camera_id: str) -> str:
    """Build the DynamoDB sort key for a camera item."""
    return f"SITE#{site_id}#CAM#{camera_id}"


def build_user_sk(sub: str) -> str:
    """Build the DynamoDB sort key for a user item."""
    return f"USER#{sub}"


# ---------------------------------------------------------------------------
# Tenant operations
# ---------------------------------------------------------------------------


def list_tenants() -> list[Mapping[str, Any]]:
    """List all tenant records by scanning for items where PK = SK = TENANT#*.

    Uses a Scan with a FilterExpression. Acceptable at low tenant counts
    (< 100). For higher scale, consider a GSI keyed on entity type.

    Returns a list of tenant items.
    """
    items: list[Mapping[str, Any]] = []
    kwargs: dict[str, Any] = {
        "TableName": get_settings().data_table,
        "FilterExpression": "begins_with(PK, :prefix) AND begins_with(SK, :prefix)",
        "ExpressionAttributeValues": {
            ":prefix": {"S": "TENANT#"},
        },
    }

    while True:
        response = _dynamodb_client().scan(**kwargs)
        # Only include items where PK == SK (tenant records, not site/camera/flag items)
        for item in response.get("Items", []):
            if item.get("PK") == item.get("SK"):
                items.append(item)
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key

    return items


def update_tenant_logo(tenant_id: str, logo_url: str) -> None:
    """Update the logo_url attribute on a tenant record.

    Sets logo_url = <logo_url> on the tenant item identified by
    (TENANT#<tenant_id>, TENANT#<tenant_id>).
    """
    _dynamodb_client().update_item(
        TableName=get_settings().data_table,
        Key={
            "PK": {"S": build_tenant_pk(tenant_id)},
            "SK": {"S": build_tenant_sk(tenant_id)},
        },
        UpdateExpression="SET logo_url = :logo_url",
        ExpressionAttributeValues={
            ":logo_url": {"S": logo_url},
        },
    )


# ---------------------------------------------------------------------------
# Site listing operations
# ---------------------------------------------------------------------------


def list_sites_for_tenant(tenant_id: str) -> list[Mapping[str, Any]]:
    """List all site records for a tenant.

    Queries PK = TENANT#<tenant_id> with SK begins_with SITE# and filters
    to only site-level items (SK = SITE#<id>, not SITE#<id>#CAM#<cam>).

    Returns a list of site items.
    """
    response: dict[str, Any] = _dynamodb_client().query(
        TableName=get_settings().data_table,
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
        ExpressionAttributeValues={
            ":pk": {"S": build_tenant_pk(tenant_id)},
            ":sk_prefix": {"S": "SITE#"},
        },
    )
    # Filter to site-level items only (exclude camera items which have #CAM# in SK)
    items = [
        item for item in response.get("Items", [])
        if "#CAM#" not in item.get("SK", {}).get("S", "")
    ]
    return items


def build_img_sk(site_id: str, camera_id: str, snapshot_ts: str) -> str:
    """Build the DynamoDB sort key for an IMG# record."""
    return f"IMG#{site_id}#{camera_id}#{snapshot_ts}"


# ---------------------------------------------------------------------------
# DynamoDB operations
# ---------------------------------------------------------------------------


def get_camera(
    tenant_id: str,
    site_id: str,
    camera_id: str,
) -> Mapping[str, Any] | None:
    """Fetch the camera item from DynamoDB.

    Single GetItem keyed by (TENANT#<tenant_id>, SITE#<site_id>#CAM#<camera_id>).
    Returns None if the item does not exist.

    Requirements: 2.3, 3.2
    """
    response: dict[str, Any] = _dynamodb_client().get_item(
        TableName=get_settings().data_table,
        Key={
            "PK": {"S": build_tenant_pk(tenant_id)},
            "SK": {"S": build_camera_sk(site_id, camera_id)},
        },
    )
    item: Mapping[str, Any] | None = response.get("Item")
    return item


def get_camera_by_token(token: str) -> Mapping[str, Any] | None:
    """Fetch the camera item by ingest token via GSI1.

    Queries GSI1 with GSI1PK = TOKEN#<token>. Returns the first matching
    item, or None if no camera has this token.

    The camera row must have GSI1PK = TOKEN#<token> and GSI1SK = CAMERA
    written at provisioning time.
    """
    response: dict[str, Any] = _dynamodb_client().query(
        TableName=get_settings().data_table,
        IndexName="GSI1",
        KeyConditionExpression="GSI1PK = :pk",
        ExpressionAttributeValues={
            ":pk": {"S": f"TOKEN#{token}"},
        },
        Limit=1,
    )
    items = response.get("Items", [])
    if not items:
        return None
    return items[0]


def parse_camera_item_ids(camera_item: Mapping[str, Any]) -> tuple[str, str, str]:
    """Extract tenant_id, site_id, camera_id from a camera item's PK/SK.

    PK format: TENANT#<tenant_id>
    SK format: SITE#<site_id>#CAM#<camera_id>
    """
    pk = camera_item["PK"]["S"]  # TENANT#acme
    sk = camera_item["SK"]["S"]  # SITE#site_01#CAM#cam_01

    tenant_id = pk.removeprefix("TENANT#")

    # SK = SITE#<site_id>#CAM#<camera_id>
    sk_without_prefix = sk.removeprefix("SITE#")
    site_id, camera_id = sk_without_prefix.split("#CAM#", 1)

    return tenant_id, site_id, camera_id


def get_retention_years(tenant_id: str) -> int:
    """Fetch the retention_years attribute from the tenant item.

    Returns _RETENTION_YEARS_DEFAULT (5) and logs a warning if the item is
    missing or the attribute is absent.

    Requirements: 5.6
    """
    response = _dynamodb_client().get_item(
        TableName=get_settings().data_table,
        Key={
            "PK": {"S": build_tenant_pk(tenant_id)},
            "SK": {"S": build_tenant_sk(tenant_id)},
        },
    )
    item = response.get("Item")
    if item is None or "retention_years" not in item:
        logger.warning(
            "tenant_row_missing_or_incomplete",
            extra={
                "tenant_id": tenant_id,
                "defaulted_retention_years": _RETENTION_YEARS_DEFAULT,
            },
        )
        return _RETENTION_YEARS_DEFAULT
    return int(item["retention_years"]["N"])


def build_site_sk(site_id: str) -> str:
    """Build the DynamoDB sort key for a site item."""
    return f"SITE#{site_id}"


def get_site(tenant_id: str, site_id: str) -> Mapping[str, Any] | None:
    """Fetch the site metadata item from DynamoDB.

    Single GetItem keyed by (TENANT#<tenant_id>, SITE#<site_id>).
    Returns None if the item does not exist.

    Requirements: 3.1
    """
    response: dict[str, Any] = _dynamodb_client().get_item(
        TableName=get_settings().data_table,
        Key={
            "PK": {"S": build_tenant_pk(tenant_id)},
            "SK": {"S": build_site_sk(site_id)},
        },
    )
    return response.get("Item")


def get_cameras_for_site(tenant_id: str, site_id: str) -> list[Mapping[str, Any]]:
    """Fetch all camera items for a site using a Query with SK prefix.

    Queries PK=TENANT#<tenant_id> with SK begins_with SITE#<site_id>#CAM#.
    Returns a list of camera items (may be empty).

    Requirements: 3.1
    """
    sk_prefix = f"SITE#{site_id}#CAM#"
    response: dict[str, Any] = _dynamodb_client().query(
        TableName=get_settings().data_table,
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
        ExpressionAttributeValues={
            ":pk": {"S": build_tenant_pk(tenant_id)},
            ":sk_prefix": {"S": sk_prefix},
        },
    )
    return response.get("Items", [])


def get_latest_img_record(
    tenant_id: str,
    site_id: str,
    camera_id: str,
) -> Mapping[str, Any] | None:
    """Fetch the most recent IMG# record for a camera.

    Queries PK=TENANT#<tenant_id> with SK begins_with IMG#<site_id>#<camera_id>#,
    sorted descending, limit 1.  Returns None if no records exist.

    Requirements: 3.1, 3.4, 3.5, 6.1, 6.2, 6.3, 6.4
    """
    sk_prefix = f"IMG#{site_id}#{camera_id}#"
    response: dict[str, Any] = _dynamodb_client().query(
        TableName=get_settings().data_table,
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
        ExpressionAttributeValues={
            ":pk": {"S": build_tenant_pk(tenant_id)},
            ":sk_prefix": {"S": sk_prefix},
        },
        ScanIndexForward=False,
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0] if items else None


def list_img_records(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    from_ts: str,
    to_ts: str,
    limit: int = 50,
    exclusive_start_key: dict | None = None,
) -> tuple[list[Mapping[str, Any]], dict | None]:
    """Query IMG# records for a camera within a time range.

    Returns (items, last_evaluated_key).
    last_evaluated_key is None when there are no more pages.

    Requirements: 4.4, 4.5, 4.7
    """
    sk_from = f"IMG#{site_id}#{camera_id}#{from_ts}"
    sk_to = f"IMG#{site_id}#{camera_id}#{to_ts}"

    kwargs: dict[str, Any] = {
        "TableName": get_settings().data_table,
        "KeyConditionExpression": "PK = :pk AND SK BETWEEN :sk_from AND :sk_to",
        "ExpressionAttributeValues": {
            ":pk": {"S": build_tenant_pk(tenant_id)},
            ":sk_from": {"S": sk_from},
            ":sk_to": {"S": sk_to},
        },
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if exclusive_start_key:
        kwargs["ExclusiveStartKey"] = exclusive_start_key

    response = _dynamodb_client().query(**kwargs)
    return response.get("Items", []), response.get("LastEvaluatedKey")


def build_flag_sk(site_id: str, camera_id: str, raised_at: str) -> str:
    """Build the DynamoDB sort key for a FLAG# record."""
    return f"FLAG#{site_id}#{camera_id}#{raised_at}"


def get_open_flag(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    reason: str,
) -> Mapping[str, Any] | None:
    """Check for an existing open or acknowledged flag for this camera+reason.

    Queries GSI1 for FLAGSTATUS#open and FLAGSTATUS#acknowledged, then
    filters by tenant_id, site_id, camera_id, and reason.  Returns the first
    matching item or None.

    Requirements: 7.6
    """
    for status in ("open", "acknowledged"):
        response: dict[str, Any] = _dynamodb_client().query(
            TableName=get_settings().data_table,
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :gsi1pk",
            FilterExpression=(
                "tenant_id = :tenant_id"
                " AND site_id = :site_id"
                " AND camera_id = :camera_id"
                " AND #reason_attr = :reason"
            ),
            ExpressionAttributeNames={"#reason_attr": "reason"},
            ExpressionAttributeValues={
                ":gsi1pk": {"S": f"FLAGSTATUS#{status}"},
                ":tenant_id": {"S": tenant_id},
                ":site_id": {"S": site_id},
                ":camera_id": {"S": camera_id},
                ":reason": {"S": reason},
            },
        )
        items = response.get("Items", [])
        if items:
            return items[0]
    return None


def put_flag(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    reason: str,
    note: str | None,
    raised_by: str,
    flag_id: str,
    raised_at: str,
) -> None:
    """Write a new flag record to DynamoDB.

    PK:     TENANT#<tenant_id>
    SK:     FLAG#<site_id>#<camera_id>#<raised_at>
    GSI1PK: FLAGSTATUS#open
    GSI1SK: <raised_at>

    Requirements: 7.5
    """
    item: dict[str, Any] = {
        "PK": {"S": build_tenant_pk(tenant_id)},
        "SK": {"S": build_flag_sk(site_id, camera_id, raised_at)},
        "GSI1PK": {"S": "FLAGSTATUS#open"},
        "GSI1SK": {"S": raised_at},
        "flag_id": {"S": flag_id},
        "tenant_id": {"S": tenant_id},
        "site_id": {"S": site_id},
        "camera_id": {"S": camera_id},
        "reason": {"S": reason},
        "status": {"S": "open"},
        "source": {"S": "user"},
        "raised_by": {"S": raised_by},
        "raised_at": {"S": raised_at},
    }
    if note is not None:
        item["note"] = {"S": note}

    _dynamodb_client().put_item(
        TableName=get_settings().data_table,
        Item=item,
    )


def list_flags(
    status_list: list[str],
    tenant_id: str | None = None,
    site_id: str | None = None,
    camera_id: str | None = None,
    limit: int = 50,
    exclusive_start_key: dict | None = None,
) -> tuple[list[Mapping[str, Any]], dict | None]:
    """List flags filtered by status and optional tenant/site/camera.

    Queries GSI1 for each status in status_list.
    Returns (items, last_evaluated_key).

    For each status, queries GSI1PK = FLAGSTATUS#<status> and applies
    optional FilterExpression for tenant_id, site_id, camera_id.

    Requirements: 8.1, 8.2, 8.3, 8.4, 8.5
    """
    all_items: list[Mapping[str, Any]] = []
    last_key: dict | None = None

    for status in status_list:
        filter_parts: list[str] = []
        expr_attr_values: dict[str, Any] = {
            ":gsi1pk": {"S": f"FLAGSTATUS#{status}"},
        }
        expr_attr_names: dict[str, str] = {}

        if tenant_id is not None:
            filter_parts.append("tenant_id = :tenant_id")
            expr_attr_values[":tenant_id"] = {"S": tenant_id}

        if site_id is not None:
            filter_parts.append("site_id = :site_id")
            expr_attr_values[":site_id"] = {"S": site_id}

        if camera_id is not None:
            filter_parts.append("camera_id = :camera_id")
            expr_attr_values[":camera_id"] = {"S": camera_id}

        kwargs: dict[str, Any] = {
            "TableName": get_settings().data_table,
            "IndexName": "GSI1",
            "KeyConditionExpression": "GSI1PK = :gsi1pk",
            "ExpressionAttributeValues": expr_attr_values,
            "ScanIndexForward": False,
        }

        if filter_parts:
            kwargs["FilterExpression"] = " AND ".join(filter_parts)

        if expr_attr_names:
            kwargs["ExpressionAttributeNames"] = expr_attr_names

        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key

        response = _dynamodb_client().query(**kwargs)
        items = response.get("Items", [])
        all_items.extend(items)

        # Track the last evaluated key from the last status queried
        if response.get("LastEvaluatedKey"):
            last_key = response["LastEvaluatedKey"]

    # Sort all merged items by raised_at descending
    all_items.sort(
        key=lambda item: item.get("raised_at", {}).get("S", ""),
        reverse=True,
    )

    # Apply limit
    if len(all_items) > limit:
        # Return limit items and signal there are more
        return all_items[:limit], {"_overflow": True}

    return all_items, last_key


def get_flag_by_id(tenant_id: str, flag_id: str) -> Mapping[str, Any] | None:
    """Find a flag by its flag_id within a tenant.

    Queries PK=TENANT#<tenant_id> with SK begins_with FLAG#, then filters
    by flag_id attribute.  Returns the first matching item or None.

    Requirements: 8.6, 8.7
    """
    response = _dynamodb_client().query(
        TableName=get_settings().data_table,
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
        FilterExpression="flag_id = :flag_id",
        ExpressionAttributeValues={
            ":pk": {"S": build_tenant_pk(tenant_id)},
            ":sk_prefix": {"S": "FLAG#"},
            ":flag_id": {"S": flag_id},
        },
    )
    items = response.get("Items", [])
    return items[0] if items else None


def update_flag_status(
    pk: str,
    sk: str,
    new_status: str,
    admin_notes: str | None,
    acting_user: str,
    updated_at: str,
) -> None:
    """Update a flag's status, GSI1PK, and optional admin_notes.

    Requirements: 8.6, 8.7
    """
    update_expr = (
        "SET #status = :status, GSI1PK = :gsi1pk,"
        " acting_user = :acting_user, updated_at = :updated_at"
    )
    expr_attr_names = {"#status": "status"}
    expr_attr_values: dict[str, Any] = {
        ":status": {"S": new_status},
        ":gsi1pk": {"S": f"FLAGSTATUS#{new_status}"},
        ":acting_user": {"S": acting_user},
        ":updated_at": {"S": updated_at},
    }
    if admin_notes is not None:
        update_expr += ", admin_notes = :admin_notes"
        expr_attr_values[":admin_notes"] = {"S": admin_notes}

    _dynamodb_client().update_item(
        TableName=get_settings().data_table,
        Key={"PK": {"S": pk}, "SK": {"S": sk}},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_attr_names,
        ExpressionAttributeValues=expr_attr_values,
    )


def put_img_record(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    snapshot_ts: str,
    s3_key: str,
    sha256_hex: str,
    size_bytes: int,
    weather: dict[str, dict[str, str]] | None = None,
) -> None:
    """Write an IMG# record to DynamoDB (unconditional PutItem).

    No ConditionExpression — duplicates overwrite (idempotent, P3).

    Args:
        weather: Optional DynamoDB map attribute (from weather_to_dynamo_map).

    Requirements: 6.1, 6.2, 7.2
    """
    item: dict[str, Any] = {
        "PK": {"S": build_tenant_pk(tenant_id)},
        "SK": {"S": build_img_sk(site_id, camera_id, snapshot_ts)},
        "s3_key": {"S": s3_key},
        "sha256": {"S": sha256_hex},
        "size_bytes": {"N": str(size_bytes)},
        "ingested_at": {"S": snapshot_ts},
        "content_type": {"S": "image/jpeg"},
    }
    if weather is not None:
        item["weather"] = weather

    _dynamodb_client().put_item(
        TableName=get_settings().data_table,
        Item=item,
    )


# ---------------------------------------------------------------------------
# Tenant write/read operations (admin management)
# ---------------------------------------------------------------------------


def get_tenant(tenant_id: str) -> Mapping[str, Any] | None:
    """Fetch a tenant record from DynamoDB.

    Single GetItem keyed by (TENANT#<tenant_id>, TENANT#<tenant_id>).
    Returns None if the item does not exist.

    Requirements: 2.5 (verify tenant exists before site creation)
    """
    response: dict[str, Any] = _dynamodb_client().get_item(
        TableName=get_settings().data_table,
        Key={
            "PK": {"S": build_tenant_pk(tenant_id)},
            "SK": {"S": build_tenant_sk(tenant_id)},
        },
    )
    return response.get("Item")


def put_tenant(
    tenant_id: str,
    tenant_name: str,
    primary_contact_email: str,
    stale_threshold_hours: int,
) -> Mapping[str, Any]:
    """Write a new tenant record to DynamoDB with uniqueness enforcement.

    Uses ConditionExpression attribute_not_exists(PK) to prevent overwriting
    an existing tenant. Raises botocore ClientError with code
    ConditionalCheckFailedException if the tenant already exists.

    Returns the written item as a plain dict (not DynamoDB-typed).

    Requirements: 1.1, 1.9
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    item: dict[str, Any] = {
        "PK": {"S": build_tenant_pk(tenant_id)},
        "SK": {"S": build_tenant_sk(tenant_id)},
        "tenant_name": {"S": tenant_name},
        "primary_contact_email": {"S": primary_contact_email},
        "stale_threshold_hours": {"N": str(stale_threshold_hours)},
        "created_at": {"S": now},
    }

    _dynamodb_client().put_item(
        TableName=get_settings().data_table,
        Item=item,
        ConditionExpression="attribute_not_exists(PK)",
    )

    return {
        "tenant_id": tenant_id,
        "tenant_name": tenant_name,
        "primary_contact_email": primary_contact_email,
        "stale_threshold_hours": stale_threshold_hours,
    }


# ---------------------------------------------------------------------------
# Site write operations (admin management)
# ---------------------------------------------------------------------------


def put_site(
    tenant_id: str,
    site_id: str,
    site_name: str,
    latitude: float,
    longitude: float,
    timezone_str: str,
) -> Mapping[str, Any]:
    """Write a new site record to DynamoDB with uniqueness enforcement.

    Uses ConditionExpression attribute_not_exists(PK) AND attribute_not_exists(SK)
    to prevent overwriting an existing site within the tenant.
    Raises botocore ClientError with code ConditionalCheckFailedException
    if the site already exists.

    Returns the written item as a plain dict (not DynamoDB-typed).

    Requirements: 2.12, 2.13
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    item: dict[str, Any] = {
        "PK": {"S": build_tenant_pk(tenant_id)},
        "SK": {"S": build_site_sk(site_id)},
        "site_name": {"S": site_name},
        "latitude": {"N": str(latitude)},
        "longitude": {"N": str(longitude)},
        "timezone": {"S": timezone_str},
        "created_at": {"S": now},
    }

    _dynamodb_client().put_item(
        TableName=get_settings().data_table,
        Item=item,
        ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
    )

    return {
        "site_id": site_id,
        "site_name": site_name,
        "tenant_id": tenant_id,
        "latitude": latitude,
        "longitude": longitude,
        "timezone": timezone_str,
    }


# ---------------------------------------------------------------------------
# Camera write/delete operations (admin management)
# ---------------------------------------------------------------------------


def put_camera(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    camera_name: str,
    camera_model: str | None,
    ingest_token: str,
) -> Mapping[str, Any]:
    """Write a new camera record to DynamoDB with uniqueness enforcement.

    Uses ConditionExpression attribute_not_exists(PK) AND attribute_not_exists(SK)
    to prevent overwriting an existing camera on the site.
    Raises botocore ClientError with code ConditionalCheckFailedException
    if the camera already exists.

    The ingest_token is written as GSI1PK = TOKEN#<token>, GSI1SK = CAMERA
    so the ingest handler can look up the camera by token via GSI1.

    Returns the written item as a plain dict (not DynamoDB-typed).

    Requirements: 3.11, 3.12
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    item: dict[str, Any] = {
        "PK": {"S": build_tenant_pk(tenant_id)},
        "SK": {"S": build_camera_sk(site_id, camera_id)},
        "GSI1PK": {"S": f"TOKEN#{ingest_token}"},
        "GSI1SK": {"S": "CAMERA"},
        "camera_name": {"S": camera_name},
        "ingest_token": {"S": ingest_token},
        "created_at": {"S": now},
    }
    if camera_model is not None:
        item["camera_model"] = {"S": camera_model}

    _dynamodb_client().put_item(
        TableName=get_settings().data_table,
        Item=item,
        ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
    )

    result: dict[str, Any] = {
        "camera_id": camera_id,
        "camera_name": camera_name,
    }
    if camera_model is not None:
        result["camera_model"] = camera_model
    return result


def update_camera_token(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    new_token: str,
) -> None:
    """Update a camera's ingest token in DynamoDB.

    Overwrites the GSI1PK, GSI1SK, and ingest_token attributes so the
    ingest handler resolves the new token to this camera. The old token
    becomes immediately invalid (no GSI1 entry points to it).

    Requirements: 6.1–6.10
    """
    _dynamodb_client().update_item(
        TableName=get_settings().data_table,
        Key={
            "PK": {"S": build_tenant_pk(tenant_id)},
            "SK": {"S": build_camera_sk(site_id, camera_id)},
        },
        UpdateExpression=(
            "SET GSI1PK = :gsi1pk, GSI1SK = :gsi1sk, ingest_token = :token"
        ),
        ExpressionAttributeValues={
            ":gsi1pk": {"S": f"TOKEN#{new_token}"},
            ":gsi1sk": {"S": "CAMERA"},
            ":token": {"S": new_token},
        },
    )


def update_camera(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    updates: dict[str, Any],
) -> None:
    """Update mutable camera attributes (camera_name, camera_model).

    Builds a dynamic UpdateExpression from the provided fields. A value of
    None for camera_model removes the attribute. The key attributes (PK/SK),
    ingest_token, and GSI1 mapping are never touched here.
    """
    set_parts: list[str] = []
    remove_parts: list[str] = []
    attr_values: dict[str, Any] = {}
    attr_names: dict[str, str] = {}

    for key, value in updates.items():
        name_placeholder = f"#{key}"
        attr_names[name_placeholder] = key
        if value is None:
            remove_parts.append(name_placeholder)
        else:
            value_placeholder = f":{key}"
            set_parts.append(f"{name_placeholder} = {value_placeholder}")
            attr_values[value_placeholder] = {"S": str(value)}

    if not set_parts and not remove_parts:
        return

    clauses: list[str] = []
    if set_parts:
        clauses.append(f"SET {', '.join(set_parts)}")
    if remove_parts:
        clauses.append(f"REMOVE {', '.join(remove_parts)}")

    kwargs: dict[str, Any] = {
        "TableName": get_settings().data_table,
        "Key": {
            "PK": {"S": build_tenant_pk(tenant_id)},
            "SK": {"S": build_camera_sk(site_id, camera_id)},
        },
        "UpdateExpression": " ".join(clauses),
        "ExpressionAttributeNames": attr_names,
    }
    if attr_values:
        kwargs["ExpressionAttributeValues"] = attr_values

    _dynamodb_client().update_item(**kwargs)


def delete_camera(tenant_id: str, site_id: str, camera_id: str) -> None:
    """Delete a camera record from DynamoDB.

    Used for rollback scenarios when Secrets Manager write fails after
    the DynamoDB camera record has been written.

    Requirements: 3.14
    """
    _dynamodb_client().delete_item(
        TableName=get_settings().data_table,
        Key={
            "PK": {"S": build_tenant_pk(tenant_id)},
            "SK": {"S": build_camera_sk(site_id, camera_id)},
        },
    )


# ---------------------------------------------------------------------------
# User operations
# ---------------------------------------------------------------------------


def put_user(
    tenant_id: str,
    sub: str,
    email: str,
    full_name: str,
    role: str,
    site_access: list[str],
) -> None:
    """Write a User_Record to DynamoDB.

    PK: TENANT#<tenant_id>
    SK: USER#<sub>

    No ConditionExpression — the sub is unique from Cognito, and
    re-writes are acceptable (idempotent).

    Requirements: 1.1, 1.2
    """
    item: dict[str, Any] = {
        "PK": {"S": build_tenant_pk(tenant_id)},
        "SK": {"S": build_user_sk(sub)},
        "sub": {"S": sub},
        "email": {"S": email},
        "full_name": {"S": full_name},
        "tenant_id": {"S": tenant_id},
        "role": {"S": role},
        "site_access": {"L": [{"S": sid} for sid in site_access]},
    }

    _dynamodb_client().put_item(
        TableName=get_settings().data_table,
        Item=item,
    )


def get_users_for_tenant(tenant_id: str) -> list[Mapping[str, Any]]:
    """Fetch all User_Records for a tenant.

    Queries PK=TENANT#<tenant_id> with SK begins_with USER#.
    Paginates through all results and returns the complete list.

    Requirements: 2.6
    """
    items: list[Mapping[str, Any]] = []
    kwargs: dict[str, Any] = {
        "TableName": get_settings().data_table,
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk_prefix)",
        "ExpressionAttributeValues": {
            ":pk": {"S": build_tenant_pk(tenant_id)},
            ":sk_prefix": {"S": "USER#"},
        },
    }

    while True:
        response = _dynamodb_client().query(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key

    return items


# ---------------------------------------------------------------------------
# Snapshot delete operations
# ---------------------------------------------------------------------------


def get_img_record_by_key(tenant_id: str, img_sk: str) -> Mapping[str, Any] | None:
    """Fetch an IMG# record by its exact PK/SK.

    Returns None if the item does not exist.
    """
    response: dict[str, Any] = _dynamodb_client().get_item(
        TableName=get_settings().data_table,
        Key={
            "PK": {"S": build_tenant_pk(tenant_id)},
            "SK": {"S": img_sk},
        },
    )
    return response.get("Item")


def delete_img_record(tenant_id: str, img_sk: str) -> None:
    """Delete an IMG# record from DynamoDB."""
    _dynamodb_client().delete_item(
        TableName=get_settings().data_table,
        Key={
            "PK": {"S": build_tenant_pk(tenant_id)},
            "SK": {"S": img_sk},
        },
    )


# ---------------------------------------------------------------------------
# Site update operations
# ---------------------------------------------------------------------------


def update_site(
    tenant_id: str,
    site_id: str,
    updates: dict[str, Any],
) -> None:
    """Update one or more attributes on a site record.

    Supported fields: ingest_hours, latitude, longitude, timezone.
    Builds a dynamic UpdateExpression based on which fields are present.
    """
    # Handle ingest_hours separately (it has remove logic)
    if "ingest_hours" in updates and updates["ingest_hours"] is None:
        # Remove ingest_hours, then process remaining fields
        remaining = {k: v for k, v in updates.items() if k != "ingest_hours"}
        if remaining:
            # Build SET + REMOVE in one call
            set_parts: list[str] = []
            attr_values: dict[str, Any] = {}
            for key, value in remaining.items():
                placeholder = f":val_{key}"
                set_parts.append(f"{key} = {placeholder}")
                attr_values[placeholder] = _to_dynamo_value(key, value)

            _dynamodb_client().update_item(
                TableName=get_settings().data_table,
                Key={
                    "PK": {"S": build_tenant_pk(tenant_id)},
                    "SK": {"S": build_site_sk(site_id)},
                },
                UpdateExpression=f"SET {', '.join(set_parts)} REMOVE ingest_hours",
                ExpressionAttributeValues=attr_values,
            )
        else:
            _dynamodb_client().update_item(
                TableName=get_settings().data_table,
                Key={
                    "PK": {"S": build_tenant_pk(tenant_id)},
                    "SK": {"S": build_site_sk(site_id)},
                },
                UpdateExpression="REMOVE ingest_hours",
            )
        return

    # All fields are SET operations
    set_parts = []
    attr_values: dict[str, Any] = {}

    for key, value in updates.items():
        placeholder = f":val_{key}"
        set_parts.append(f"{key} = {placeholder}")
        attr_values[placeholder] = _to_dynamo_value(key, value)

    if not set_parts:
        return

    _dynamodb_client().update_item(
        TableName=get_settings().data_table,
        Key={
            "PK": {"S": build_tenant_pk(tenant_id)},
            "SK": {"S": build_site_sk(site_id)},
        },
        UpdateExpression=f"SET {', '.join(set_parts)}",
        ExpressionAttributeValues=attr_values,
    )


def _to_dynamo_value(key: str, value: Any) -> dict[str, Any]:
    """Convert a Python value to its DynamoDB attribute representation."""
    if key == "ingest_hours" and isinstance(value, dict):
        return {
            "M": {
                "start": {"S": value["start"]},
                "end": {"S": value["end"]},
            }
        }
    if key in ("latitude", "longitude"):
        return {"N": str(value)}
    if key == "timezone":
        return {"S": value}
    # Fallback: treat as string
    return {"S": str(value)}


# ---------------------------------------------------------------------------
# Site ingest hours operations
# ---------------------------------------------------------------------------


def update_site_ingest_hours(
    tenant_id: str,
    site_id: str,
    ingest_hours: dict[str, str] | None,
) -> None:
    """Update the ingest_hours attribute on a site record.

    If ingest_hours is None, removes the attribute (all hours allowed).
    If ingest_hours is a dict with 'start' and 'end', sets the attribute
    as a Map with S-typed start/end values.
    """
    if ingest_hours is None:
        # Remove the attribute
        _dynamodb_client().update_item(
            TableName=get_settings().data_table,
            Key={
                "PK": {"S": build_tenant_pk(tenant_id)},
                "SK": {"S": build_site_sk(site_id)},
            },
            UpdateExpression="REMOVE ingest_hours",
        )
    else:
        _dynamodb_client().update_item(
            TableName=get_settings().data_table,
            Key={
                "PK": {"S": build_tenant_pk(tenant_id)},
                "SK": {"S": build_site_sk(site_id)},
            },
            UpdateExpression="SET ingest_hours = :ih",
            ExpressionAttributeValues={
                ":ih": {
                    "M": {
                        "start": {"S": ingest_hours["start"]},
                        "end": {"S": ingest_hours["end"]},
                    }
                },
            },
        )


def get_site_ingest_hours(tenant_id: str, site_id: str) -> dict[str, str] | None:
    """Fetch the ingest_hours config for a site.

    Returns None if no ingest_hours are configured (all hours allowed).
    Returns {"start": "HH:MM", "end": "HH:MM"} if configured.
    """
    site_item = get_site(tenant_id, site_id)
    if site_item is None:
        return None

    ingest_hours_attr = site_item.get("ingest_hours")
    if ingest_hours_attr is None:
        return None

    # DynamoDB Map type: {"M": {"start": {"S": "07:00"}, "end": {"S": "18:00"}}}
    m = ingest_hours_attr.get("M")
    if m is None:
        return None

    start = m.get("start", {}).get("S")
    end = m.get("end", {}).get("S")

    if start and end:
        return {"start": start, "end": end}

    return None


# ---------------------------------------------------------------------------
# Live view session operations (SESSION# records)
# ---------------------------------------------------------------------------


def build_session_sk(site_id: str, camera_id: str) -> str:
    """Build the DynamoDB sort key for a SESSION# record.

    Requirements (live-view): 2.1, 2.13
    """
    return f"SESSION#{site_id}#{camera_id}"


def get_live_session(
    tenant_id: str,
    site_id: str,
    camera_id: str,
) -> Mapping[str, Any] | None:
    """Fetch the SESSION# record for a camera.

    Single GetItem keyed by (TENANT#<tenant_id>, SESSION#<site_id>#<camera_id>).
    Returns None if the item does not exist.

    Requirements (live-view): 2.4, 2.6, 4.1, 6.2, 6.4
    """
    response: dict[str, Any] = _dynamodb_client().get_item(
        TableName=get_settings().data_table,
        Key={
            "PK": {"S": build_tenant_pk(tenant_id)},
            "SK": {"S": build_session_sk(site_id, camera_id)},
        },
    )
    return response.get("Item")


def put_live_session(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    session_id: str,
    expires_at: str,
    ttl: int,
    created_by: str,
    created_at: str,
    now_ts: str,
) -> None:
    """Write a SESSION# record to DynamoDB with duplicate prevention.

    The ConditionExpression allows the write only when no SESSION# record
    exists OR the existing one has already expired. A genuinely active session
    (``expires_at`` in the future) causes a ConditionalCheckFailedException so
    that a second concurrent POST cannot clobber it.

    Allowing overwrite of an *expired* record is required because DynamoDB TTL
    deletion is lazy (an expired item can physically linger for up to ~48h).
    Without this, a stale-but-unreaped session would block all new sessions
    for the camera until DynamoDB happened to reap it.

    ``now_ts`` and ``expires_at`` are ISO 8601 UTC strings in the fixed
    ``%Y-%m-%dT%H:%M:%SZ`` format, so lexicographic comparison is equivalent
    to chronological comparison.

    Requirements (live-view): 2.1, 2.4, 2.5, 2.11, 2.13, 6.2, 6.5
    """
    _dynamodb_client().put_item(
        TableName=get_settings().data_table,
        Item={
            "PK": {"S": build_tenant_pk(tenant_id)},
            "SK": {"S": build_session_sk(site_id, camera_id)},
            "session_id": {"S": session_id},
            "expires_at": {"S": expires_at},
            "ttl": {"N": str(ttl)},
            "created_by": {"S": created_by},
            "created_at": {"S": created_at},
        },
        ConditionExpression="attribute_not_exists(SK) OR expires_at <= :now",
        ExpressionAttributeValues={":now": {"S": now_ts}},
    )


def delete_live_session(tenant_id: str, site_id: str, camera_id: str) -> None:
    """Delete the SESSION# record for a camera.

    DeleteItem by (TENANT#<tenant_id>, SESSION#<site_id>#<camera_id>).
    No-ops silently if the item does not exist.

    Requirements (live-view): 4.1, 6.4
    """
    _dynamodb_client().delete_item(
        TableName=get_settings().data_table,
        Key={
            "PK": {"S": build_tenant_pk(tenant_id)},
            "SK": {"S": build_session_sk(site_id, camera_id)},
        },
    )


# ---------------------------------------------------------------------------
# LIVE_IMG# key builder and record operations  (live-view-session feature)
# ---------------------------------------------------------------------------


def build_live_img_sk(site_id: str, camera_id: str, snapshot_ts: str) -> str:
    """Build the DynamoDB sort key for a LIVE_IMG# record.

    Returns: ``LIVE_IMG#<site_id>#<camera_id>#<snapshot_ts>``

    Requirements (live-view): 3.1, 5.6, 6.4
    """
    return f"LIVE_IMG#{site_id}#{camera_id}#{snapshot_ts}"


def get_latest_live_img_record(
    tenant_id: str,
    site_id: str,
    camera_id: str,
) -> Mapping[str, Any] | None:
    """Fetch the most recent LIVE_IMG# record for a camera.

    Queries ``PK=TENANT#<tenant_id>`` with ``SK begins_with LIVE_IMG#<site_id>#<camera_id>#``,
    sorted descending, limit 1.  Returns ``None`` if no records exist.

    Requirements (live-view): 3.1, 5.6, 6.4
    """
    sk_prefix = f"LIVE_IMG#{site_id}#{camera_id}#"
    response: dict[str, Any] = _dynamodb_client().query(
        TableName=get_settings().data_table,
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
        ExpressionAttributeValues={
            ":pk": {"S": build_tenant_pk(tenant_id)},
            ":sk_prefix": {"S": sk_prefix},
        },
        ScanIndexForward=False,
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0] if items else None


def put_live_img_record(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    snapshot_ts: str,
    s3_key: str,
    sha256_hex: str,
    size_bytes: int,
    ttl: int,
) -> None:
    """Write a LIVE_IMG# record to DynamoDB (unconditional PutItem).

    Record schema:

    +--------------+-------+-------------------------------------------+
    | Attribute    | Type  | Value                                     |
    +==============+=======+===========================================+
    | PK           | S     | TENANT#<tenant_id>                        |
    | SK           | S     | LIVE_IMG#<site_id>#<camera_id>#<ts>       |
    | s3_key       | S     | live/<tenant>/<site>/<cam>/<ts>.jpg       |
    | sha256       | S     | hex SHA-256 of the JPEG body              |
    | size_bytes   | N     | byte length of the JPEG body              |
    | captured_at  | S     | ISO 8601 UTC timestamp (<snapshot_ts>)    |
    | ttl          | N     | Unix epoch of captured_at + 3600          |
    +--------------+-------+-------------------------------------------+

    No ConditionExpression — concurrent live writes overwrite (idempotent).

    Requirements (live-view): 3.1, 5.6, 6.4
    """
    _dynamodb_client().put_item(
        TableName=get_settings().data_table,
        Item={
            "PK": {"S": build_tenant_pk(tenant_id)},
            "SK": {"S": build_live_img_sk(site_id, camera_id, snapshot_ts)},
            "s3_key": {"S": s3_key},
            "sha256": {"S": sha256_hex},
            "size_bytes": {"N": str(size_bytes)},
            "captured_at": {"S": snapshot_ts},
            "ttl": {"N": str(ttl)},
        },
    )

# ---------------------------------------------------------------------------
# Sandbox provisioning and camera transfer operations (camera-sandbox feature)
# ---------------------------------------------------------------------------

_SANDBOX_TENANT_ID = "sandbox_construction"
_SANDBOX_TENANT_NAME = "Sandbox Construction"
_SANDBOX_STALE_THRESHOLD_HOURS = 24
_SANDBOX_DEFAULT_SITE_ID = "default_sandbox_site"
_SANDBOX_DEFAULT_SITE_NAME = "Default Sandbox Site"
_SANDBOX_DEFAULT_LATITUDE = -33.8688
_SANDBOX_DEFAULT_LONGITUDE = 151.2093
_SANDBOX_DEFAULT_TIMEZONE = "Australia/Sydney"


def ensure_sandbox_tenant_record() -> bool:
    """Create the sandbox tenant record if it doesn't exist.

    Uses conditional PutItem: attribute_not_exists(PK).
    Returns True if created, False if already existed.

    Requirements (camera-sandbox): 1.2, 1.3
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    item: dict[str, Any] = {
        "PK": {"S": build_tenant_pk(_SANDBOX_TENANT_ID)},
        "SK": {"S": build_tenant_sk(_SANDBOX_TENANT_ID)},
        "tenant_name": {"S": _SANDBOX_TENANT_NAME},
        "stale_threshold_hours": {"N": str(_SANDBOX_STALE_THRESHOLD_HOURS)},
        "created_at": {"S": now},
    }
    try:
        _dynamodb_client().put_item(
            TableName=get_settings().data_table,
            Item=item,
            ConditionExpression="attribute_not_exists(PK)",
        )
        return True
    except _dynamodb_client().exceptions.ConditionalCheckFailedException:
        return False


def ensure_sandbox_default_site() -> bool:
    """Create the default sandbox site record if it doesn't exist.

    Uses conditional PutItem: attribute_not_exists(PK) AND attribute_not_exists(SK).
    Returns True if created, False if already existed.

    Requirements (camera-sandbox): 1.4
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    item: dict[str, Any] = {
        "PK": {"S": build_tenant_pk(_SANDBOX_TENANT_ID)},
        "SK": {"S": build_site_sk(_SANDBOX_DEFAULT_SITE_ID)},
        "site_name": {"S": _SANDBOX_DEFAULT_SITE_NAME},
        "latitude": {"N": str(_SANDBOX_DEFAULT_LATITUDE)},
        "longitude": {"N": str(_SANDBOX_DEFAULT_LONGITUDE)},
        "timezone": {"S": _SANDBOX_DEFAULT_TIMEZONE},
        "created_at": {"S": now},
    }
    try:
        _dynamodb_client().put_item(
            TableName=get_settings().data_table,
            Item=item,
            ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
        )
        return True
    except _dynamodb_client().exceptions.ConditionalCheckFailedException:
        return False


def transfer_camera(
    source_tenant_id: str,
    source_site_id: str,
    target_tenant_id: str,
    target_site_id: str,
    camera_id: str,
    camera_name: str,
    camera_model: str | None,
    ingest_token: str,
    created_at: str,
) -> None:
    """Atomically move a camera from source to target using transact_write_items.

    Transaction items (in order):
    1. Put — create camera at target with ConditionExpression
       attribute_not_exists(PK) AND attribute_not_exists(SK)
    2. Delete — remove camera at source with ConditionExpression
       attribute_exists(PK)

    The Put item includes a ``transferred_at`` timestamp recording when the
    transfer occurred. The original ``created_at`` is preserved.

    If either condition fails, the entire transaction is rejected and no
    changes are applied.

    Raises:
        botocore.exceptions.ClientError: On transaction failure (e.g., conflict
        at target or source already deleted by concurrent request).

    Requirements (camera-sandbox): 5.3, 5.4, 5.5, 5.6, 7.1
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    table_name = get_settings().data_table

    target_item: dict[str, Any] = {
        "PK": {"S": build_tenant_pk(target_tenant_id)},
        "SK": {"S": build_camera_sk(target_site_id, camera_id)},
        "GSI1PK": {"S": f"TOKEN#{ingest_token}"},
        "GSI1SK": {"S": "CAMERA"},
        "camera_name": {"S": camera_name},
        "ingest_token": {"S": ingest_token},
        "created_at": {"S": created_at},
        "transferred_at": {"S": now},
    }
    if camera_model is not None:
        target_item["camera_model"] = {"S": camera_model}

    transact_items = [
        {
            "Put": {
                "TableName": table_name,
                "Item": target_item,
                "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)",
            }
        },
        {
            "Delete": {
                "TableName": table_name,
                "Key": {
                    "PK": {"S": build_tenant_pk(source_tenant_id)},
                    "SK": {"S": build_camera_sk(source_site_id, camera_id)},
                },
                "ConditionExpression": "attribute_exists(PK)",
            }
        },
    ]

    _dynamodb_client().transact_write_items(TransactItems=transact_items)
