"""Example/edge tests for the reworked ingest handler (working-hours retention).

These tests cover concrete edge cases of the 24/7-ingestion + retention-class
rework of ``POST /v1/ingest/{token}``:

- Invalid JPEG body → 400 and no snapshot record persisted (Req 4.7)
- Missing / invalid / expired token → 401 and no snapshot record persisted (Req 4.8)
- Weather enrichment only on In_Hours snapshots with lat/lon present (Req 4.6)
- Live-session capture preserved alongside retention classification (Req 4.5)
- Unresolvable capture time on the Out_Of_Hours path → error, no record (Req 7.2)
- Missing timezone → saved as Out_Of_Hours with a logged reason (Req 3.10)

Requirements validated: 3.10, 4.5, 4.6, 4.7, 4.8, 7.2
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import boto3
from moto import mock_aws

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT_ID = "acme"
_SITE_ID = "site_01"
_CAMERA_ID = "cam_01"
_VALID_TOKEN = "tk_validtoken1234567890abcdefghijklmnopqr"
_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"\x00" * 128


# ---------------------------------------------------------------------------
# Environment / AWS setup helpers (mirror the existing ingest test conventions)
# ---------------------------------------------------------------------------


def _set_env() -> None:
    os.environ["SNAPSHOTS_BUCKET"] = "test-snapshots-bucket"
    os.environ["DATA_TABLE"] = "test-data-table"
    os.environ["AWS_REGION"] = "eu-west-2"
    os.environ["AWS_DEFAULT_REGION"] = "eu-west-2"
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["ENVIRONMENT"] = "test"
    os.environ["LOG_LEVEL"] = "INFO"

    from sitespy.config import get_settings
    from sitespy.data import _dynamodb_client
    from sitespy.storage import _s3_client

    get_settings.cache_clear()
    _dynamodb_client.cache_clear()
    _s3_client.cache_clear()


def _setup_aws():
    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(
        Bucket="test-snapshots-bucket",
        CreateBucketConfiguration={"LocationConstraint": "eu-west-2"},
    )
    s3.put_bucket_versioning(
        Bucket="test-snapshots-bucket",
        VersioningConfiguration={"Status": "Enabled"},
    )
    ddb = boto3.client("dynamodb", region_name="eu-west-2")
    ddb.create_table(
        TableName="test-data-table",
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
    return s3, ddb


def _seed_camera(ddb, ingest_token=_VALID_TOKEN, retention_years=5) -> None:
    ddb.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": f"TENANT#{_TENANT_ID}"},
            "SK": {"S": f"SITE#{_SITE_ID}#CAM#{_CAMERA_ID}"},
            "GSI1PK": {"S": f"TOKEN#{ingest_token}"},
            "GSI1SK": {"S": "CAMERA"},
            "camera_name": {"S": "Test Camera"},
            "ingest_token": {"S": ingest_token},
        },
    )
    ddb.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": f"TENANT#{_TENANT_ID}"},
            "SK": {"S": f"TENANT#{_TENANT_ID}"},
            "retention_years": {"N": str(retention_years)},
        },
    )


def _seed_site(
    ddb,
    timezone: str | None = None,
    working_hours: dict | None = None,
    latitude: str | None = None,
    longitude: str | None = None,
) -> None:
    """Write a SITE# metadata record with optional tz / working_hours / lat / lon."""
    item: dict = {
        "PK": {"S": f"TENANT#{_TENANT_ID}"},
        "SK": {"S": f"SITE#{_SITE_ID}"},
        "site_name": {"S": "Test Site"},
    }
    if timezone is not None:
        item["timezone"] = {"S": timezone}
    if latitude is not None:
        item["latitude"] = {"N": latitude}
    if longitude is not None:
        item["longitude"] = {"N": longitude}
    if working_hours is not None:
        days = working_hours.get("days")
        wh_map: dict = {
            "start": {"S": working_hours["start"]},
            "end": {"S": working_hours["end"]},
        }
        if days is not None:
            wh_map["days"] = {"L": [{"S": d} for d in days]}
        item["working_hours"] = {"M": wh_map}
    ddb.put_item(TableName="test-data-table", Item=item)


def _seed_active_session(ddb) -> None:
    """Write an active SESSION# record (expires 5 minutes in the future)."""
    expires_at = (datetime.now(tz=UTC) + timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    ddb.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": f"TENANT#{_TENANT_ID}"},
            "SK": {"S": f"SESSION#{_SITE_ID}#{_CAMERA_ID}"},
            "session_id": {"S": "test-session-id"},
            "expires_at": {"S": expires_at},
            "ttl": {"N": "9999999999"},
        },
    )


def _make_event(token=_VALID_TOKEN, body=_JPEG_BODY) -> dict:
    return {
        "httpMethod": "POST",
        "path": f"/v1/ingest/{token}",
        "pathParameters": {"token": token},
        "headers": {"Content-Type": "image/jpeg"},
        "queryStringParameters": None,
        "body": base64.b64encode(body).decode(),
        "isBase64Encoded": True,
    }


def _count_snapshot_records(ddb) -> int:
    """Return the count of persisted IMG# / OOH_IMG# / LIVE_IMG# records."""
    resp = ddb.query(
        TableName="test-data-table",
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": f"TENANT#{_TENANT_ID}"}},
    )
    prefixes = ("IMG#", "OOH_IMG#", "LIVE_IMG#")
    return sum(
        1
        for item in resp.get("Items", [])
        if item["SK"]["S"].startswith(prefixes)
    )


# ---------------------------------------------------------------------------
# Req 4.7 — invalid JPEG → 400, no record persisted
# ---------------------------------------------------------------------------


def test_invalid_jpeg_returns_400_and_persists_no_record():
    """A body that fails JPEG magic-byte validation is rejected 400 with no write."""
    from sitespy.errors import ApiError
    from sitespy.handlers.ingest import _handle, resolve_correlation_id
    from sitespy.http import error_response

    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb)
        _seed_site(ddb, timezone="Europe/London")

        event = _make_event(body=b"\x00\x01\x02\x03not-a-jpeg")
        corr_id = resolve_correlation_id(event)
        try:
            result = _handle(event, corr_id)
        except ApiError as exc:
            result = error_response(exc, corr_id)

        assert result["statusCode"] == 400
        assert _count_snapshot_records(ddb) == 0


# ---------------------------------------------------------------------------
# Req 4.8 — missing / invalid / expired token → 401, no record persisted
# ---------------------------------------------------------------------------


def test_missing_token_returns_401_and_persists_no_record():
    """An empty token in the path is rejected 401 with no write."""
    from sitespy.errors import ApiError
    from sitespy.handlers.ingest import _handle, resolve_correlation_id
    from sitespy.http import error_response

    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb)
        _seed_site(ddb, timezone="Europe/London")

        event = _make_event(token="")
        corr_id = resolve_correlation_id(event)
        try:
            result = _handle(event, corr_id)
        except ApiError as exc:
            result = error_response(exc, corr_id)

        assert result["statusCode"] == 401
        assert _count_snapshot_records(ddb) == 0


def test_malformed_token_returns_401_and_persists_no_record():
    """A token that does not match the tk_ format is rejected 401 with no write."""
    from sitespy.errors import ApiError
    from sitespy.handlers.ingest import _handle, resolve_correlation_id
    from sitespy.http import error_response

    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb)
        _seed_site(ddb, timezone="Europe/London")

        event = _make_event(token="not-a-valid-token")
        corr_id = resolve_correlation_id(event)
        try:
            result = _handle(event, corr_id)
        except ApiError as exc:
            result = error_response(exc, corr_id)

        assert result["statusCode"] == 401
        assert _count_snapshot_records(ddb) == 0


def test_unknown_token_returns_401_and_persists_no_record():
    """A well-formed token with no matching camera row is rejected 401 with no write."""
    from sitespy.errors import ApiError
    from sitespy.handlers.ingest import _handle, resolve_correlation_id
    from sitespy.http import error_response

    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        # Seed a camera bound to a DIFFERENT token so the presented token is unknown.
        _seed_camera(ddb, ingest_token=_VALID_TOKEN)
        _seed_site(ddb, timezone="Europe/London")

        other_token = "tk_" + "z" * 40
        event = _make_event(token=other_token)
        corr_id = resolve_correlation_id(event)
        try:
            result = _handle(event, corr_id)
        except ApiError as exc:
            result = error_response(exc, corr_id)

        assert result["statusCode"] == 401
        assert _count_snapshot_records(ddb) == 0


# ---------------------------------------------------------------------------
# Req 4.6 — weather enrichment only on In_Hours snapshots with lat/lon present
# ---------------------------------------------------------------------------


def test_in_hours_with_latlon_enriches_weather():
    """In_Hours snapshot + lat/lon present → weather fetched and stored on the record."""
    from sitespy.handlers.ingest import _handle

    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb)
        # No working_hours + no ingest_hours → classifier returns In_Hours.
        _seed_site(ddb, timezone="Europe/London", latitude="51.5", longitude="-0.12")

        with (
            patch(
                "sitespy.handlers.ingest.fetch_current_weather",
                return_value={"temp_c": 12.0},
            ) as mock_fetch,
            patch(
                "sitespy.handlers.ingest.weather_to_dynamo_map",
                return_value={"M": {"temp_c": {"N": "12.0"}}},
            ),
        ):
            result = _handle(_make_event(), "corr-weather-in")

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["retention_class"] == "In_Hours"
        mock_fetch.assert_called_once()

        img_sk = f"IMG#{_SITE_ID}#{_CAMERA_ID}#{body['timestamp']}"
        item = ddb.get_item(
            TableName="test-data-table",
            Key={"PK": {"S": f"TENANT#{_TENANT_ID}"}, "SK": {"S": img_sk}},
        ).get("Item")
        assert item is not None
        assert "weather" in item


def test_in_hours_without_latlon_skips_weather():
    """In_Hours snapshot without lat/lon → weather is not fetched and not stored."""
    from sitespy.handlers.ingest import _handle

    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb)
        _seed_site(ddb, timezone="Europe/London")  # no lat/lon

        with patch(
            "sitespy.handlers.ingest.fetch_current_weather"
        ) as mock_fetch:
            result = _handle(_make_event(), "corr-weather-nolatlon")

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["retention_class"] == "In_Hours"
        mock_fetch.assert_not_called()

        img_sk = f"IMG#{_SITE_ID}#{_CAMERA_ID}#{body['timestamp']}"
        item = ddb.get_item(
            TableName="test-data-table",
            Key={"PK": {"S": f"TENANT#{_TENANT_ID}"}, "SK": {"S": img_sk}},
        ).get("Item")
        assert item is not None
        assert "weather" not in item


def test_out_of_hours_does_not_enrich_weather_even_with_latlon():
    """Out_Of_Hours snapshot never fetches weather, even when lat/lon are present."""
    from sitespy.handlers.ingest import _handle

    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb)
        # working_hours present but timezone missing → classifier → Out_Of_Hours.
        _seed_site(
            ddb,
            timezone=None,
            working_hours={"start": "09:00", "end": "17:00"},
            latitude="51.5",
            longitude="-0.12",
        )

        with patch(
            "sitespy.handlers.ingest.fetch_current_weather"
        ) as mock_fetch:
            result = _handle(_make_event(), "corr-weather-ooh")

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["retention_class"] == "Out_Of_Hours"
        mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Req 4.5 — live-session capture preserved alongside retention classification
# ---------------------------------------------------------------------------


def test_live_session_capture_preserved():
    """An active live session captures a live/ frame alongside the timelapse write."""
    from sitespy.handlers.ingest import _handle

    _set_env()
    with mock_aws():
        s3, ddb = _setup_aws()
        _seed_camera(ddb)
        _seed_site(ddb, timezone="Europe/London")
        _seed_active_session(ddb)

        result = _handle(_make_event(), "corr-live")

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["live_captured"] is True

        # A LIVE_IMG# record was persisted under the live/ prefix.
        resp = ddb.query(
            TableName="test-data-table",
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": {"S": f"TENANT#{_TENANT_ID}"},
                ":sk": {"S": f"LIVE_IMG#{_SITE_ID}#{_CAMERA_ID}#"},
            },
        )
        live_items = resp.get("Items", [])
        assert len(live_items) == 1
        assert live_items[0]["s3_key"]["S"].startswith("live/")


# ---------------------------------------------------------------------------
# Req 7.2 — unresolvable capture time on Out_Of_Hours path → error, no record
# ---------------------------------------------------------------------------


def test_unresolvable_capture_time_returns_error_and_persists_no_record():
    """When the Out_Of_Hours capture time cannot be resolved, the write is rejected
    and no snapshot record is persisted (Req 7.2)."""
    from sitespy.errors import ApiError
    from sitespy.handlers.ingest import _handle, resolve_correlation_id
    from sitespy.http import error_response

    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb)
        # working_hours present but timezone missing → Out_Of_Hours path.
        _seed_site(
            ddb, timezone=None, working_hours={"start": "09:00", "end": "17:00"}
        )

        event = _make_event()
        corr_id = resolve_correlation_id(event)
        with patch(
            "sitespy.handlers.ingest._capture_epoch_seconds", return_value=None
        ):
            try:
                result = _handle(event, corr_id)
            except ApiError as exc:
                result = error_response(exc, corr_id)

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert _count_snapshot_records(ddb) == 0


# ---------------------------------------------------------------------------
# Req 3.10 — missing timezone → saved as Out_Of_Hours with a logged reason
# ---------------------------------------------------------------------------


def test_missing_timezone_saved_as_out_of_hours_with_logged_reason():
    """A site with working_hours but no timezone classifies as Out_Of_Hours, still
    persists the snapshot, and logs the invalid_timezone reason (Req 3.10)."""
    from sitespy.handlers.ingest import _handle

    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb)
        _seed_site(
            ddb, timezone=None, working_hours={"start": "09:00", "end": "17:00"}
        )

        with patch("sitespy.handlers.ingest.logger.warning") as mock_warning:
            result = _handle(_make_event(), "corr-notz")

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["retention_class"] == "Out_Of_Hours"

        # An OOH_IMG# record was persisted (24/7 ingestion — snapshot not discarded).
        resp = ddb.query(
            TableName="test-data-table",
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": {"S": f"TENANT#{_TENANT_ID}"},
                ":sk": {"S": f"OOH_IMG#{_SITE_ID}#{_CAMERA_ID}#"},
            },
        )
        assert len(resp.get("Items", [])) == 1

        # The classifier's invalid_timezone reason was logged.
        logged_reasons = [
            call.kwargs.get("extra", {}).get("reason")
            for call in mock_warning.call_args_list
        ]
        assert "invalid_timezone" in logged_reasons
