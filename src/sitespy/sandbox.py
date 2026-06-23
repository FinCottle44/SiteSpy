"""Sandbox tenant utilities for SiteSpy.

Provides constants, provisioning logic, and visibility guard for the
hidden sandbox tenant (sandbox_construction) used by super_admins to
stage and test cameras before customer handover.

Requirements validated: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3
"""

from __future__ import annotations

import logging

from sitespy.errors import Forbidden, InternalError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Well-known sandbox constants
# ---------------------------------------------------------------------------

SANDBOX_TENANT_ID = "sandbox_construction"
SANDBOX_TENANT_NAME = "Sandbox Construction"
SANDBOX_STALE_THRESHOLD_HOURS = 24
SANDBOX_DEFAULT_SITE_ID = "default_sandbox_site"
SANDBOX_DEFAULT_SITE_NAME = "Default Sandbox Site"
SANDBOX_DEFAULT_LATITUDE = -33.8688  # Sydney, AU
SANDBOX_DEFAULT_LONGITUDE = 151.2093
SANDBOX_DEFAULT_TIMEZONE = "Australia/Sydney"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_sandbox_tenant(tenant_id: str) -> bool:
    """Return True if tenant_id matches the well-known sandbox tenant ID."""
    return tenant_id == SANDBOX_TENANT_ID


def sandbox_visibility_guard(tenant_id: str, role: str) -> None:
    """Raise Forbidden if a non-super_admin attempts to access the sandbox tenant.

    Args:
        tenant_id: The tenant being accessed.
        role: The caller's resolved role ("super_admin", "tenant_admin", "user").

    Raises:
        Forbidden: If tenant_id is the sandbox and role is not super_admin.
    """
    if is_sandbox_tenant(tenant_id) and role != "super_admin":
        raise Forbidden("You do not have access to this resource.")


def ensure_sandbox_tenant() -> None:
    """Idempotently ensure the sandbox tenant and default site exist.

    If the tenant record does not exist, creates it along with the default site.
    If it already exists, returns immediately without modification.

    Uses conditional PutItem (attribute_not_exists(PK)) so concurrent calls
    are safe — only one wins, the rest no-op.

    Raises:
        InternalError: If provisioning fails due to a DynamoDB error.
    """
    from sitespy import data

    try:
        data.ensure_sandbox_tenant_record()
    except Exception as exc:
        logger.exception("sandbox_tenant_provisioning_failed")
        raise InternalError("Failed to provision sandbox tenant.") from exc

    try:
        data.ensure_sandbox_default_site()
    except Exception as exc:
        logger.exception("sandbox_default_site_provisioning_failed")
        raise InternalError("Failed to provision sandbox default site.") from exc
