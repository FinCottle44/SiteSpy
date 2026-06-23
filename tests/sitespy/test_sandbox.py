"""Unit tests for sitespy.sandbox module.

Validates constants, is_sandbox_tenant, sandbox_visibility_guard, and
ensure_sandbox_tenant.

Requirements validated: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3
"""

from __future__ import annotations

import pytest
from moto import mock_aws

from sitespy.errors import Forbidden, InternalError
from sitespy.sandbox import (
    SANDBOX_DEFAULT_LATITUDE,
    SANDBOX_DEFAULT_LONGITUDE,
    SANDBOX_DEFAULT_SITE_ID,
    SANDBOX_DEFAULT_SITE_NAME,
    SANDBOX_DEFAULT_TIMEZONE,
    SANDBOX_STALE_THRESHOLD_HOURS,
    SANDBOX_TENANT_ID,
    SANDBOX_TENANT_NAME,
    ensure_sandbox_tenant,
    is_sandbox_tenant,
    sandbox_visibility_guard,
)


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


class TestSandboxConstants:
    """Verify sandbox constants match the design spec."""

    def test_tenant_id(self) -> None:
        assert SANDBOX_TENANT_ID == "sandbox_construction"

    def test_tenant_name(self) -> None:
        assert SANDBOX_TENANT_NAME == "Sandbox Construction"

    def test_stale_threshold_hours(self) -> None:
        assert SANDBOX_STALE_THRESHOLD_HOURS == 24

    def test_default_site_id(self) -> None:
        assert SANDBOX_DEFAULT_SITE_ID == "default_sandbox_site"

    def test_default_site_name(self) -> None:
        assert SANDBOX_DEFAULT_SITE_NAME == "Default Sandbox Site"

    def test_default_latitude(self) -> None:
        assert SANDBOX_DEFAULT_LATITUDE == -33.8688

    def test_default_longitude(self) -> None:
        assert SANDBOX_DEFAULT_LONGITUDE == 151.2093

    def test_default_timezone(self) -> None:
        assert SANDBOX_DEFAULT_TIMEZONE == "Australia/Sydney"


# ---------------------------------------------------------------------------
# is_sandbox_tenant tests
# ---------------------------------------------------------------------------


class TestIsSandboxTenant:
    """Verify is_sandbox_tenant identification logic."""

    def test_returns_true_for_sandbox_tenant_id(self) -> None:
        assert is_sandbox_tenant("sandbox_construction") is True

    def test_returns_false_for_other_tenant_id(self) -> None:
        assert is_sandbox_tenant("acme_corp") is False

    def test_returns_false_for_empty_string(self) -> None:
        assert is_sandbox_tenant("") is False

    def test_returns_false_for_partial_match(self) -> None:
        assert is_sandbox_tenant("sandbox") is False
        assert is_sandbox_tenant("sandbox_construction_extra") is False


# ---------------------------------------------------------------------------
# sandbox_visibility_guard tests
# ---------------------------------------------------------------------------


class TestSandboxVisibilityGuard:
    """Verify sandbox_visibility_guard raises Forbidden appropriately."""

    def test_raises_forbidden_for_tenant_admin(self) -> None:
        with pytest.raises(Forbidden) as exc_info:
            sandbox_visibility_guard("sandbox_construction", "tenant_admin")
        assert exc_info.value.message == "You do not have access to this resource."

    def test_raises_forbidden_for_user_role(self) -> None:
        with pytest.raises(Forbidden):
            sandbox_visibility_guard("sandbox_construction", "user")

    def test_does_not_raise_for_super_admin(self) -> None:
        # Should not raise
        sandbox_visibility_guard("sandbox_construction", "super_admin")

    def test_does_not_raise_for_non_sandbox_tenant(self) -> None:
        # Non-sandbox tenant should never raise, regardless of role
        sandbox_visibility_guard("acme_corp", "user")
        sandbox_visibility_guard("acme_corp", "tenant_admin")
        sandbox_visibility_guard("acme_corp", "super_admin")


# ---------------------------------------------------------------------------
# ensure_sandbox_tenant tests
# ---------------------------------------------------------------------------


class TestEnsureSandboxTenant:
    """Verify ensure_sandbox_tenant provisioning logic with moto DynamoDB."""

    def test_creates_tenant_and_site_when_not_exist(self, moto_dynamodb) -> None:
        """Calling ensure_sandbox_tenant creates both the tenant and default site."""
        from sitespy import data

        # Clear the cached client so it picks up the moto mock
        data._dynamodb_client.cache_clear()

        ensure_sandbox_tenant()

        # Verify tenant record exists
        tenant = data.get_tenant("sandbox_construction")
        assert tenant is not None
        assert tenant["tenant_name"]["S"] == "Sandbox Construction"
        assert tenant["stale_threshold_hours"]["N"] == "24"
        assert "created_at" in tenant

        # Verify default site record exists
        site = data.get_site("sandbox_construction", "default_sandbox_site")
        assert site is not None
        assert site["site_name"]["S"] == "Default Sandbox Site"
        assert site["latitude"]["N"] == str(-33.8688)
        assert site["longitude"]["N"] == str(151.2093)
        assert site["timezone"]["S"] == "Australia/Sydney"

    def test_idempotent_second_call(self, moto_dynamodb) -> None:
        """Calling ensure_sandbox_tenant twice does not error or modify records."""
        from sitespy import data

        data._dynamodb_client.cache_clear()

        ensure_sandbox_tenant()
        # Get the created_at from first call
        tenant_first = data.get_tenant("sandbox_construction")
        site_first = data.get_site("sandbox_construction", "default_sandbox_site")

        # Second call should not raise
        ensure_sandbox_tenant()

        # Records should be unchanged
        tenant_second = data.get_tenant("sandbox_construction")
        site_second = data.get_site("sandbox_construction", "default_sandbox_site")

        assert tenant_first["created_at"] == tenant_second["created_at"]
        assert site_first["created_at"] == site_second["created_at"]
