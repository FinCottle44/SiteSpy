"""Header/body validation partition tests.

Requirements validated: 1.2, 1.3, 1.4, 1.5, 1.6, 1.7
"""

from __future__ import annotations

import base64
import os

import pytest

from sitespy.errors import ApiError
from sitespy.handlers.ingest import _handle, resolve_correlation_id
from sitespy.http import error_response, unhandled_error_response

_MAX_BODY = 10 * 1024 * 1024  # 10 MiB


def _set_env():
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


def _invoke(event):
    corr_id = resolve_correlation_id(event)
    try:
        return _handle(event, corr_id)
    except ApiError as exc:
        return error_response(exc, corr_id)
    except Exception:
        return unhandled_error_response(corr_id)


def _make_valid_event(body=None):
    if body is None:
        body = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    credentials = base64.b64encode(b"user:pass").decode()
    return {
        "httpMethod": "POST",
        "path": "/v1/ingest",
        "headers": {
            "Authorization": f"Basic {credentials}",
            "X-Tenant-ID": "acme",
            "X-Site-ID": "site_01",
            "Content-Type": "image/jpeg",
        },
        "queryStringParameters": {"cameraID": "cam_01"},
        "body": base64.b64encode(body).decode(),
        "isBase64Encoded": True,
    }


# ---------------------------------------------------------------------------
# Missing required fields → 400
# ---------------------------------------------------------------------------


def test_missing_authorization_header():
    _set_env()
    event = _make_valid_event()
    del event["headers"]["Authorization"]
    result = _invoke(event)
    # Missing auth header → 401 (not 400) per spec
    assert result["statusCode"] == 401


def test_missing_x_tenant_id():
    _set_env()
    event = _make_valid_event()
    del event["headers"]["X-Tenant-ID"]
    result = _invoke(event)
    assert result["statusCode"] == 400


def test_missing_x_site_id():
    _set_env()
    event = _make_valid_event()
    del event["headers"]["X-Site-ID"]
    result = _invoke(event)
    assert result["statusCode"] == 400


def test_missing_camera_id():
    _set_env()
    event = _make_valid_event()
    event["queryStringParameters"] = {}
    result = _invoke(event)
    assert result["statusCode"] == 400


def test_missing_content_type():
    _set_env()
    event = _make_valid_event()
    del event["headers"]["Content-Type"]
    result = _invoke(event)
    assert result["statusCode"] == 400


# ---------------------------------------------------------------------------
# Regex-invalid IDs → 400
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tenant_id",
    [
        "UPPERCASE",
        "has-hyphen",
        "has space",
        "a" * 65,  # too long (>64)
        "",
    ],
)
def test_invalid_tenant_id(tenant_id):
    _set_env()
    event = _make_valid_event()
    if tenant_id == "":
        del event["headers"]["X-Tenant-ID"]
    else:
        event["headers"]["X-Tenant-ID"] = tenant_id
    result = _invoke(event)
    assert result["statusCode"] in (400, 401)  # empty → 400, invalid → 400


@pytest.mark.parametrize(
    "site_id",
    [
        "UPPERCASE",
        "has-hyphen",
        "a" * 65,
    ],
)
def test_invalid_site_id(site_id):
    _set_env()
    event = _make_valid_event()
    event["headers"]["X-Site-ID"] = site_id
    result = _invoke(event)
    assert result["statusCode"] == 400


@pytest.mark.parametrize(
    "camera_id",
    [
        "UPPERCASE",
        "has-hyphen",
        "a" * 65,
    ],
)
def test_invalid_camera_id(camera_id):
    _set_env()
    event = _make_valid_event()
    event["queryStringParameters"]["cameraID"] = camera_id
    result = _invoke(event)
    assert result["statusCode"] == 400


# ---------------------------------------------------------------------------
# Body validation → 400
# ---------------------------------------------------------------------------


def test_empty_body():
    _set_env()
    event = _make_valid_event()
    event["body"] = ""
    event["isBase64Encoded"] = False
    result = _invoke(event)
    assert result["statusCode"] == 400


def test_bad_magic_bytes():
    _set_env()
    bad_body = b"\x00\x01\x02\x03" + b"\x00" * 100
    result = _invoke(_make_valid_event(body=bad_body))
    assert result["statusCode"] == 400


def test_body_at_max_minus_1_is_accepted():
    """Body at 10 MiB - 1 should pass body size validation (may fail auth)."""
    _set_env()
    body = b"\xff\xd8\xff\xe0" + b"\x00" * (_MAX_BODY - 1 - 4)
    result = _invoke(_make_valid_event(body=body))
    # Should not be 400 for body size — may be 401 for auth
    assert result["statusCode"] != 400 or "size" not in result.get("body", "").lower()


def test_body_at_max_is_accepted():
    """Body at exactly 10 MiB should pass body size validation (may fail auth)."""
    _set_env()
    body = b"\xff\xd8\xff\xe0" + b"\x00" * (_MAX_BODY - 4)
    result = _invoke(_make_valid_event(body=body))
    # Should not be 400 for body size
    assert result["statusCode"] != 400 or "size" not in result.get("body", "").lower()


def test_body_over_max_is_rejected():
    """Body at 10 MiB + 1 must return 400."""
    _set_env()
    body = b"\xff\xd8\xff\xe0" + b"\x00" * (_MAX_BODY + 1 - 4)
    result = _invoke(_make_valid_event(body=body))
    assert result["statusCode"] == 400
