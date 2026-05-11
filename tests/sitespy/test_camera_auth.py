"""Unit tests for camera_auth.py.

Requirements validated: 2.1, 2.2, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10
"""

from __future__ import annotations

import base64
import hmac

import bcrypt
import pytest

from sitespy.camera_auth import parse_basic_auth, verify
from sitespy.errors import Unauthorized

_AUTH_FAILURE_MSG = "Authentication failed."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(auth_header: str | None) -> dict:
    headers = {}
    if auth_header is not None:
        headers["Authorization"] = auth_header
    return {"headers": headers}


def _basic_header(credentials: str) -> str:
    return "Basic " + base64.b64encode(credentials.encode()).decode()


def _make_camera_item(
    username: str = "sitespy_cam_abc",
    password: str = "s3cr3t",
    cost: int = 12,
    include_username: bool = True,
    include_hash: bool = True,
) -> dict:
    item: dict = {}
    if include_username:
        item["ingest_username"] = {"S": username}
    if include_hash:
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=cost))
        item["ingest_password_hash"] = {"S": hashed.decode()}
    return item


# ---------------------------------------------------------------------------
# parse_basic_auth — malformed input partition
# ---------------------------------------------------------------------------


def test_parse_basic_auth_missing_header():
    with pytest.raises(Unauthorized) as exc_info:
        parse_basic_auth({"headers": {}})
    assert exc_info.value.message == _AUTH_FAILURE_MSG


def test_parse_basic_auth_wrong_scheme():
    event = _make_event("Bearer sometoken")
    with pytest.raises(Unauthorized) as exc_info:
        parse_basic_auth(event)
    assert exc_info.value.message == _AUTH_FAILURE_MSG


def test_parse_basic_auth_bad_base64():
    event = _make_event("Basic not-valid-base64!!!")
    with pytest.raises(Unauthorized) as exc_info:
        parse_basic_auth(event)
    assert exc_info.value.message == _AUTH_FAILURE_MSG


def test_parse_basic_auth_empty_decoded():
    event = _make_event("Basic " + base64.b64encode(b"").decode())
    with pytest.raises(Unauthorized) as exc_info:
        parse_basic_auth(event)
    assert exc_info.value.message == _AUTH_FAILURE_MSG


def test_parse_basic_auth_zero_colons():
    event = _make_event("Basic " + base64.b64encode(b"usernameonly").decode())
    with pytest.raises(Unauthorized) as exc_info:
        parse_basic_auth(event)
    assert exc_info.value.message == _AUTH_FAILURE_MSG


def test_parse_basic_auth_two_colons():
    event = _make_event("Basic " + base64.b64encode(b"user:pass:extra").decode())
    with pytest.raises(Unauthorized) as exc_info:
        parse_basic_auth(event)
    assert exc_info.value.message == _AUTH_FAILURE_MSG


def test_parse_basic_auth_happy_path():
    event = _make_event(_basic_header("myuser:mypassword"))
    username, password = parse_basic_auth(event)
    assert username == "myuser"
    assert password == "mypassword"


# ---------------------------------------------------------------------------
# verify — failure modes
# ---------------------------------------------------------------------------


def test_verify_camera_item_none():
    with pytest.raises(Unauthorized) as exc_info:
        verify("user", "pass", None)
    assert exc_info.value.message == _AUTH_FAILURE_MSG


def test_verify_missing_username_attr():
    item = _make_camera_item(include_username=False)
    with pytest.raises(Unauthorized) as exc_info:
        verify("user", "pass", item)
    assert exc_info.value.message == _AUTH_FAILURE_MSG


def test_verify_missing_hash_attr():
    item = _make_camera_item(include_hash=False)
    with pytest.raises(Unauthorized) as exc_info:
        verify("user", "pass", item)
    assert exc_info.value.message == _AUTH_FAILURE_MSG


def test_verify_username_mismatch():
    item = _make_camera_item(username="correct_user", password="pass")
    with pytest.raises(Unauthorized) as exc_info:
        verify("wrong_user", "pass", item)
    assert exc_info.value.message == _AUTH_FAILURE_MSG


def test_verify_hash_cost_too_low():
    item = _make_camera_item(username="user", password="pass", cost=10)
    with pytest.raises(Unauthorized) as exc_info:
        verify("user", "pass", item)
    assert exc_info.value.message == _AUTH_FAILURE_MSG


def test_verify_wrong_password():
    item = _make_camera_item(username="user", password="correct_pass", cost=12)
    with pytest.raises(Unauthorized) as exc_info:
        verify("user", "wrong_pass", item)
    assert exc_info.value.message == _AUTH_FAILURE_MSG


def test_verify_success():
    item = _make_camera_item(username="user", password="correct_pass", cost=12)
    result = verify("user", "correct_pass", item)
    assert result is None


# ---------------------------------------------------------------------------
# hmac.compare_digest spy (Requirement 2.6 / Task 8.3)
# ---------------------------------------------------------------------------


def test_verify_uses_hmac_compare_digest_with_bytes(mocker):
    """Assert hmac.compare_digest is called once with bytes-typed arguments."""
    spy = mocker.spy(hmac, "compare_digest")
    item = _make_camera_item(username="user", password="correct_pass", cost=12)
    verify("user", "correct_pass", item)
    spy.assert_called_once()
    args = spy.call_args[0]
    assert isinstance(args[0], bytes), f"Expected bytes, got {type(args[0])}"
    assert isinstance(args[1], bytes), f"Expected bytes, got {type(args[1])}"
