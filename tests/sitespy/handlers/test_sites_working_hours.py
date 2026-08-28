"""Example/edge tests for the Sites API working_hours changes.

Covers PATCH /v1/sites/{site_id} and GET /v1/sites/{site_id} behaviour for the
working-hours-retention feature:

- valid working_hours persists (1.1) + 200 echo (1.3)
- working_hours: null removes the attribute (1.2)
- legacy ingest_hours field rejected with 400 (1.4)
- out-of-hours TTL config field rejected with 400 (7.6)
- role gate: admins permitted (1.5), non-admins 403 with no change (1.6)
- legacy ingest_hours removed on working_hours write (2.4)
- missing / malformed start|end → 400 (3.3, 3.4)
- GET emits working_hours including legacy derivation (2.1, 2.2)

Requirements validated: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.4, 3.3, 3.4, 7.6
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Environment setup (must happen before importing handlers)
# ---------------------------------------------------------------------------

os.environ.setdefault("SNAPSHOTS_BUCKET", "test-snapshots-bucket")
os.environ.setdefault("DATA_TABLE", "test-data-table")
os.environ.setdefault("AWS_REGION", "eu-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "INFO")

from sitespy import data  # noqa: E402
from sitespy.data import _dynamodb_client  # noqa: E402
from sitespy.handlers.sites import handler as get_handler  # noqa: E402
from sitespy.handlers.sites_patch import handler as patch_handler  # noqa: E402

_TABLE = "test-data-table"
_TENANT = "acme_corp"
_SITE = "site_001"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_caches():
    _dynamodb_client.cache_clear()
    yield
    _dynamodb_client.cache_clear()


def _create_table(client) -> None:
    client.create_table(
        TableName=_TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
    )


def _seed_site(client, *, working_hours=None, ingest_hours=None) -> None:
    item: dict[str, Any] = {
        "PK": {"S": f"TENANT#{_TENANT}"},
        "SK": {"S": f"SITE#{_SITE}"},
        "site_name": {"S": "Acme Tower"},
        "timezone": {"S": "Europe/London"},
        "latitude": {"N": "51.5074"},
        "longitude": {"N": "-0.1278"},
    }
    if working_hours is not None:
        m: dict[str, Any] = {
            "start": {"S": working_hours["start"]},
            "end": {"S": working_hours["end"]},
        }
        if "days" in working_hours:
            m["days"] = {"L": [{"S": d} for d in working_hours["days"]]}
        item["working_hours"] = {"M": m}
    if ingest_hours is not None:
        item["ingest_hours"] = {
            "M": {
                "start": {"S": ingest_hours["start"]},
                "end": {"S": ingest_hours["end"]},
            }
        }
    client.put_item(TableName=_TABLE, Item=item)


def _patch_event(
    body: dict[str, Any] | None,
    *,
    groups: str = "TenantAdmins",
    tenant_id: str = _TENANT,
    site_access: str = "",
    query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "httpMethod": "PATCH",
        "path": f"/v1/sites/{_SITE}",
        "pathParameters": {"site_id": _SITE},
        "queryStringParameters": query_params or {},
        "headers": {"X-Correlation-Id": "test-corr-id"},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:groups": groups,
                    "custom:tenant_id": tenant_id,
                    "custom:site_access": site_access,
                }
            }
        },
    }


def _get_event(
    *,
    groups: str = "TenantAdmins",
    tenant_id: str = _TENANT,
    site_access: str = "",
) -> dict[str, Any]:
    return {
        "httpMethod": "GET",
        "path": f"/v1/sites/{_SITE}",
        "pathParameters": {"site_id": _SITE},
        "queryStringParameters": {},
        "headers": {"X-Correlation-Id": "test-corr-id"},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:groups": groups,
                    "custom:tenant_id": tenant_id,
                    "custom:site_access": site_access,
                }
            }
        },
    }


def _stored_site(client) -> dict[str, Any]:
    resp = client.get_item(
        TableName=_TABLE,
        Key={"PK": {"S": f"TENANT#{_TENANT}"}, "SK": {"S": f"SITE#{_SITE}"}},
    )
    return resp["Item"]


# ---------------------------------------------------------------------------
# PATCH working_hours — persistence + echo (Req 1.1, 1.3)
# ---------------------------------------------------------------------------


class TestWorkingHoursPersist:
    def test_valid_working_hours_persists_and_echoes(self):
        with mock_aws():
            client = boto3.client("dynamodb", region_name="eu-west-2")
            _create_table(client)
            _seed_site(client)

            wh = {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "07:00", "end": "18:00"}
            result = patch_handler(_patch_event({"working_hours": wh}), MagicMock())

            # 200 with the persisted value echoed (Req 1.3).
            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["working_hours"] == wh

            # Persisted to the record (Req 1.1).
            stored = _stored_site(client)
            assert stored["working_hours"]["M"]["start"]["S"] == "07:00"
            assert stored["working_hours"]["M"]["end"]["S"] == "18:00"
            days = [d["S"] for d in stored["working_hours"]["M"]["days"]["L"]]
            assert days == ["mon", "tue", "wed", "thu", "fri"]

    def test_omitted_days_defaults_to_all_seven(self):
        with mock_aws():
            client = boto3.client("dynamodb", region_name="eu-west-2")
            _create_table(client)
            _seed_site(client)

            result = patch_handler(
                _patch_event({"working_hours": {"start": "09:00", "end": "17:00"}}),
                MagicMock(),
            )
            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["working_hours"]["days"] == list(
                ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
            )

    def test_super_admin_permitted(self):
        """Req 1.5 — super_admin may update working_hours."""
        with mock_aws():
            client = boto3.client("dynamodb", region_name="eu-west-2")
            _create_table(client)
            _seed_site(client)

            event = _patch_event(
                {"working_hours": {"start": "07:00", "end": "18:00"}},
                groups="SuperAdmins",
                tenant_id="",
                query_params={"tenant_id": _TENANT},
            )
            result = patch_handler(event, MagicMock())
            assert result["statusCode"] == 200


# ---------------------------------------------------------------------------
# PATCH working_hours: null removes attribute (Req 1.2)
# ---------------------------------------------------------------------------


class TestWorkingHoursRemoval:
    def test_null_removes_working_hours(self):
        with mock_aws():
            client = boto3.client("dynamodb", region_name="eu-west-2")
            _create_table(client)
            _seed_site(
                client,
                working_hours={"days": ["mon"], "start": "07:00", "end": "18:00"},
            )

            result = patch_handler(
                _patch_event({"working_hours": None}), MagicMock()
            )
            assert result["statusCode"] == 200

            stored = _stored_site(client)
            assert "working_hours" not in stored


# ---------------------------------------------------------------------------
# Legacy ingest_hours removed on write (Req 2.4)
# ---------------------------------------------------------------------------


class TestLegacyMigration:
    def test_ingest_hours_removed_on_working_hours_write(self):
        with mock_aws():
            client = boto3.client("dynamodb", region_name="eu-west-2")
            _create_table(client)
            _seed_site(client, ingest_hours={"start": "08:00", "end": "16:00"})

            result = patch_handler(
                _patch_event(
                    {"working_hours": {"days": ["mon"], "start": "07:00", "end": "18:00"}}
                ),
                MagicMock(),
            )
            assert result["statusCode"] == 200

            stored = _stored_site(client)
            assert "working_hours" in stored
            assert "ingest_hours" not in stored


# ---------------------------------------------------------------------------
# Rejections leave the record unchanged
# ---------------------------------------------------------------------------


class TestWorkingHoursRejections:
    def test_ingest_hours_field_rejected(self):
        """Req 1.4 — the legacy ingest_hours field is rejected, record unchanged."""
        with mock_aws():
            client = boto3.client("dynamodb", region_name="eu-west-2")
            _create_table(client)
            _seed_site(client)
            before = _stored_site(client)

            result = patch_handler(
                _patch_event({"ingest_hours": {"start": "07:00", "end": "18:00"}}),
                MagicMock(),
            )
            assert result["statusCode"] == 400
            body = json.loads(result["body"])
            assert body["error"] == "BAD_REQUEST"
            assert "working_hours" in body["message"]
            assert _stored_site(client) == before

    def test_out_of_hours_ttl_config_rejected(self):
        """Req 7.6 — attempts to configure the OOH TTL are rejected."""
        with mock_aws():
            client = boto3.client("dynamodb", region_name="eu-west-2")
            _create_table(client)
            _seed_site(client)
            before = _stored_site(client)

            for field in ("out_of_hours_ttl", "ooh_ttl_seconds", "ttl"):
                result = patch_handler(
                    _patch_event({field: 3600}), MagicMock()
                )
                assert result["statusCode"] == 400, field
                assert _stored_site(client) == before

    def test_missing_start_rejected(self):
        """Req 3.3 — omitted start/end → 400, no change."""
        with mock_aws():
            client = boto3.client("dynamodb", region_name="eu-west-2")
            _create_table(client)
            _seed_site(client)
            before = _stored_site(client)

            result = patch_handler(
                _patch_event({"working_hours": {"end": "18:00"}}), MagicMock()
            )
            assert result["statusCode"] == 400
            assert _stored_site(client) == before

    def test_missing_end_rejected(self):
        with mock_aws():
            client = boto3.client("dynamodb", region_name="eu-west-2")
            _create_table(client)
            _seed_site(client)

            result = patch_handler(
                _patch_event({"working_hours": {"start": "07:00"}}), MagicMock()
            )
            assert result["statusCode"] == 400

    @pytest.mark.parametrize(
        "start,end",
        [
            ("25:00", "18:00"),   # hour out of range
            ("07:60", "18:00"),   # minute out of range
            ("7am", "6pm"),       # not HH:MM
            ("07:00", "24:00"),   # end out of range
            ("07:00", "18-00"),   # malformed separator
        ],
    )
    def test_malformed_start_end_rejected(self, start, end):
        """Req 3.4 — malformed / out-of-range start|end → 400, no change."""
        with mock_aws():
            client = boto3.client("dynamodb", region_name="eu-west-2")
            _create_table(client)
            _seed_site(client)
            before = _stored_site(client)

            result = patch_handler(
                _patch_event({"working_hours": {"start": start, "end": end}}),
                MagicMock(),
            )
            assert result["statusCode"] == 400, (start, end)
            assert _stored_site(client) == before


# ---------------------------------------------------------------------------
# Role gate (Req 1.6)
# ---------------------------------------------------------------------------


class TestRoleGate:
    def test_non_admin_forbidden_and_no_change(self):
        with mock_aws():
            client = boto3.client("dynamodb", region_name="eu-west-2")
            _create_table(client)
            _seed_site(client)
            before = _stored_site(client)

            event = _patch_event(
                {"working_hours": {"start": "07:00", "end": "18:00"}},
                groups="",  # regular user
                site_access=_SITE,
            )
            result = patch_handler(event, MagicMock())
            assert result["statusCode"] == 403
            body = json.loads(result["body"])
            assert body["error"] == "ACCESS_DENIED"
            assert _stored_site(client) == before


# ---------------------------------------------------------------------------
# GET emits working_hours incl. legacy derivation (Req 1.1, 2.1, 2.2)
# ---------------------------------------------------------------------------


class TestGetEmitsWorkingHours:
    def test_get_emits_explicit_working_hours(self):
        with mock_aws():
            client = boto3.client("dynamodb", region_name="eu-west-2")
            _create_table(client)
            _seed_site(
                client,
                working_hours={
                    "days": ["mon", "tue", "wed", "thu", "fri"],
                    "start": "07:00",
                    "end": "18:00",
                },
            )

            result = get_handler(_get_event(), MagicMock())
            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["working_hours"] == {
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "start": "07:00",
                "end": "18:00",
            }
            assert "ingest_hours" not in body

    def test_get_derives_working_hours_from_legacy_ingest_hours(self):
        """Req 2.1/2.2 — legacy ingest_hours derives to all-seven-days without mutation."""
        with mock_aws():
            client = boto3.client("dynamodb", region_name="eu-west-2")
            _create_table(client)
            _seed_site(client, ingest_hours={"start": "08:00", "end": "16:00"})

            result = get_handler(_get_event(), MagicMock())
            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["working_hours"] == {
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "start": "08:00",
                "end": "16:00",
            }
            assert "ingest_hours" not in body

            # Read-only derivation must not have mutated the stored record.
            stored = _stored_site(client)
            assert "working_hours" not in stored
            assert stored["ingest_hours"]["M"]["start"]["S"] == "08:00"

    def test_get_emits_null_when_unset(self):
        with mock_aws():
            client = boto3.client("dynamodb", region_name="eu-west-2")
            _create_table(client)
            _seed_site(client)

            result = get_handler(_get_event(), MagicMock())
            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["working_hours"] is None
