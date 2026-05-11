"""DynamoDB helpers for SiteSpy — key builders and table operations.

Requirements validated: 2.3, 5.6, 6.1, 6.2, 7.2, 7.5, 7.6, 8.1, 8.2, 8.3, 8.4, 8.5
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
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
) -> None:
    """Write an IMG# record to DynamoDB (unconditional PutItem).

    No ConditionExpression — duplicates overwrite (idempotent, P3).

    Requirements: 6.1, 6.2, 7.2
    """
    _dynamodb_client().put_item(
        TableName=get_settings().data_table,
        Item={
            "PK": {"S": build_tenant_pk(tenant_id)},
            "SK": {"S": build_img_sk(site_id, camera_id, snapshot_ts)},
            "s3_key": {"S": s3_key},
            "sha256": {"S": sha256_hex},
            "size_bytes": {"N": str(size_bytes)},
            "ingested_at": {"S": snapshot_ts},
            "content_type": {"S": "image/jpeg"},
        },
    )
