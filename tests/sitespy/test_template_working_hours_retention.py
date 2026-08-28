"""Configuration / smoke tests for working-hours-retention infrastructure.

These example-based tests assert the declarative infrastructure in
``sitespy/template.yaml`` for the working-hours retention feature:

- The ``ExpireOutOfHoursSnapshots`` S3 lifecycle rule targets the ``security/``
  prefix and expires objects after 7 days (Requirements 7.4).
- No lifecycle rule applies an expiration to in-hours long-term snapshots or to
  the ``preserved/`` (promoted) prefix — those are retained indefinitely
  (Requirement 6.4).
- The ``TransitionToGlacierAfter1Year`` rule still transitions objects to
  Glacier IR exactly 365 days after creation (Requirement 6.3).
- The three out-of-hours Lambda functions
  (``SnapshotsOutOfHoursFunction``/review, ``SnapshotsPromoteFunction``/promote,
  ``SnapshotsDownloadFunction``/download) exist, serve their expected routes,
  carry the expected DynamoDB + S3 policies, and inherit the API's Cognito
  default authorizer (Requirements 8.7, 9.8, 10.4).

The template uses CloudFormation intrinsic tags (``!Sub``, ``!Ref``,
``!GetAtt``, etc.).  A plain ``yaml.safe_load`` cannot parse these, so we
register a permissive multi-constructor for the ``!`` tag prefix that turns
each intrinsic into a simple sentinel string, letting us inspect the plain
structural values (prefixes, days, paths, methods, policies) we care about.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

# ---------------------------------------------------------------------------
# CloudFormation-tolerant YAML loader
# ---------------------------------------------------------------------------


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation intrinsic ``!`` tags."""


def _cfn_multi_constructor(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):
    """Collapse any ``!<Intrinsic>`` tag into a plain Python value.

    Scalars become ``"!<tag> <value>"`` strings; sequences and mappings are
    constructed normally and returned as-is.  This keeps the structural,
    non-intrinsic values (prefixes, expiration days, paths, HTTP methods,
    policy actions) directly inspectable while never raising on an unknown
    ``!`` tag.
    """
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
        return f"!{tag_suffix} {value}".strip()
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_CfnLoader.add_multi_constructor("!", _cfn_multi_constructor)


def _template_path() -> pathlib.Path:
    # tests/sitespy/<this file> -> parents[2] == sitespy/ project root.
    return pathlib.Path(__file__).resolve().parents[2] / "template.yaml"


@pytest.fixture(scope="module")
def template() -> dict:
    with _template_path().open("r", encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_CfnLoader)


@pytest.fixture(scope="module")
def resources(template: dict) -> dict:
    return template["Resources"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lifecycle_rules(resources: dict) -> list[dict]:
    """Return the S3 bucket lifecycle rules from the snapshots bucket."""
    for res in resources.values():
        if res.get("Type") != "AWS::S3::Bucket":
            continue
        config = res.get("Properties", {}).get("LifecycleConfiguration")
        if config:
            return config["Rules"]
    raise AssertionError("No S3 bucket with a LifecycleConfiguration found")


def _rule_by_id(resources: dict, rule_id: str) -> dict:
    rules = _lifecycle_rules(resources)
    matches = [r for r in rules if r.get("Id") == rule_id]
    assert matches, f"{rule_id} lifecycle rule not found"
    assert len(matches) == 1, f"Expected exactly one {rule_id} rule"
    return matches[0]


def _api_events(func: dict) -> list[dict]:
    """Return the Api-typed events of a Serverless::Function resource."""
    events = func.get("Properties", {}).get("Events", {}) or {}
    return [
        e["Properties"]
        for e in events.values()
        if e.get("Type") == "Api" and "Properties" in e
    ]


def _function_by_handler(resources: dict, handler: str) -> dict:
    for res in resources.values():
        if res.get("Type") != "AWS::Serverless::Function":
            continue
        if res.get("Properties", {}).get("Handler") == handler:
            return res
    raise AssertionError(f"No Serverless::Function with handler {handler!r} found")


def _has_dynamodb_policy(func: dict, policy_name: str) -> bool:
    policies = func["Properties"].get("Policies", [])
    return any(isinstance(p, dict) and policy_name in p for p in policies)


def _s3_actions_on_snapshots(func: dict) -> set[str]:
    """Collect S3 actions granted on the snapshots bucket for a function."""
    actions: set[str] = set()
    policies = func["Properties"].get("Policies", [])
    for policy in policies:
        if not isinstance(policy, dict) or "Statement" not in policy:
            continue
        statements = policy["Statement"]
        if isinstance(statements, dict):
            statements = [statements]
        for stmt in statements:
            stmt_actions = stmt.get("Action", [])
            if isinstance(stmt_actions, str):
                stmt_actions = [stmt_actions]
            resource = stmt.get("Resource", "")
            # Every snapshots-bucket resource ARN references SnapshotsBucket and
            # ends with a /* object glob under the !Sub intrinsic sentinel.
            if "SnapshotsBucket" in str(resource):
                actions.update(a for a in stmt_actions if a.startswith("s3:"))
    return actions


# Full handler strings for the three out-of-hours functions.
_HANDLER_REVIEW = "sitespy.handlers.snapshots_out_of_hours.handler_list"
_HANDLER_PROMOTE = "sitespy.handlers.snapshots_out_of_hours.handler_promote"
_HANDLER_DOWNLOAD = "sitespy.handlers.snapshots_out_of_hours.handler_download"


# ---------------------------------------------------------------------------
# S3 lifecycle retention
# ---------------------------------------------------------------------------


def test_expire_out_of_hours_prefix_and_days(resources: dict) -> None:
    """ExpireOutOfHoursSnapshots targets security/ and expires after 7 days."""
    rule = _rule_by_id(resources, "ExpireOutOfHoursSnapshots")
    assert rule["Prefix"] == "security/"
    assert rule["ExpirationInDays"] == 7
    assert rule["Status"] == "Enabled"


def test_out_of_hours_rule_has_no_transition(resources: dict) -> None:
    """The out-of-hours rule only expires — it declares no storage transition."""
    rule = _rule_by_id(resources, "ExpireOutOfHoursSnapshots")
    assert "Transitions" not in rule
    assert "Transition" not in rule


def test_glacier_ir_transition_remains_365_days(resources: dict) -> None:
    """TransitionToGlacierAfter1Year still moves objects to Glacier IR at 365 days."""
    rule = _rule_by_id(resources, "TransitionToGlacierAfter1Year")
    transitions = rule["Transitions"]
    assert len(transitions) == 1
    transition = transitions[0]
    assert transition["StorageClass"] == "GLACIER_IR"
    assert transition["TransitionInDays"] == 365
    # The long-term transition rule never expires objects.
    assert "ExpirationInDays" not in rule


def test_no_expiry_on_in_hours_or_preserved_prefix(resources: dict) -> None:
    """No lifecycle rule expires in-hours long-term or preserved/ objects.

    In-hours snapshots use the tenant-scoped root prefix (no leading literal
    segment) and promoted snapshots use ``preserved/``.  Every rule that
    declares an ExpirationInDays must therefore be scoped to a prefix other
    than the root and other than ``preserved/`` (i.e. live/, timelapse/, or
    security/).
    """
    expiring_prefixes = {
        rule.get("Prefix")
        for rule in _lifecycle_rules(resources)
        if "ExpirationInDays" in rule
    }
    # No expiry rule targets the promoted prefix.
    assert "preserved/" not in expiring_prefixes
    # Every expiry rule is scoped to a specific non-root prefix (never applies
    # to the in-hours long-term objects that live at the tenant root prefix).
    assert None not in expiring_prefixes
    assert "" not in expiring_prefixes
    # Only the expected prefixes carry an expiry.
    assert expiring_prefixes == {"live/", "timelapse/", "security/"}


def test_noncurrent_versions_are_expired(resources: dict) -> None:
    """The versioned bucket reclaims storage from noncurrent versions.

    Without a NoncurrentVersionExpiration rule, every ExpirationInDays rule on
    a versioned bucket only writes a delete marker and retains the object bytes
    as a noncurrent version indefinitely — so the security/ 7-day expiry (and
    the live/ and timelapse/ expiries) would never actually reclaim storage.
    """
    rule = _rule_by_id(resources, "ExpireNoncurrentVersions")
    assert rule["Status"] == "Enabled"
    assert rule["NoncurrentVersionExpiration"]["NoncurrentDays"] == 1
    # Applies bucket-wide (no prefix/filter scoping it to a subset).
    assert "Prefix" not in rule


def test_expired_delete_markers_are_removed(resources: dict) -> None:
    """Delete markers left behind by expiry rules are cleaned up bucket-wide."""
    rule = _rule_by_id(resources, "RemoveExpiredDeleteMarkers")
    assert rule["Status"] == "Enabled"
    assert rule["ExpiredObjectDeleteMarker"] is True
    assert "Prefix" not in rule


# ---------------------------------------------------------------------------
# Out-of-hours Lambda functions — existence + routes
# ---------------------------------------------------------------------------


def test_out_of_hours_functions_exist(resources: dict) -> None:
    for handler in (_HANDLER_REVIEW, _HANDLER_PROMOTE, _HANDLER_DOWNLOAD):
        func = _function_by_handler(resources, handler)
        assert func["Type"] == "AWS::Serverless::Function"


def test_review_serves_get_out_of_hours_route(resources: dict) -> None:
    func = _function_by_handler(resources, _HANDLER_REVIEW)
    routes = [(e.get("Method"), e.get("Path")) for e in _api_events(func)]
    assert ("GET", "/v1/snapshots/out-of-hours") in routes


def test_promote_serves_post_promote_route(resources: dict) -> None:
    func = _function_by_handler(resources, _HANDLER_PROMOTE)
    routes = [(e.get("Method"), e.get("Path")) for e in _api_events(func)]
    assert ("POST", "/v1/snapshots/out-of-hours/promote") in routes


def test_download_serves_get_download_route(resources: dict) -> None:
    func = _function_by_handler(resources, _HANDLER_DOWNLOAD)
    routes = [(e.get("Method"), e.get("Path")) for e in _api_events(func)]
    assert ("GET", "/v1/snapshots/out-of-hours/download") in routes


def test_out_of_hours_routes_are_distinct(resources: dict) -> None:
    """The three functions serve three disjoint routes."""
    review = {(e.get("Method"), e.get("Path")) for e in _api_events(
        _function_by_handler(resources, _HANDLER_REVIEW))}
    promote = {(e.get("Method"), e.get("Path")) for e in _api_events(
        _function_by_handler(resources, _HANDLER_PROMOTE))}
    download = {(e.get("Method"), e.get("Path")) for e in _api_events(
        _function_by_handler(resources, _HANDLER_DOWNLOAD))}
    assert review.isdisjoint(promote)
    assert review.isdisjoint(download)
    assert promote.isdisjoint(download)


# ---------------------------------------------------------------------------
# Out-of-hours Lambda functions — policies
# ---------------------------------------------------------------------------


def test_review_function_policies(resources: dict) -> None:
    """Review has a read-only DynamoDB policy and s3:GetObject only."""
    func = _function_by_handler(resources, _HANDLER_REVIEW)
    assert _has_dynamodb_policy(func, "DynamoDBReadPolicy")
    assert _s3_actions_on_snapshots(func) == {"s3:GetObject"}


def test_promote_function_policies(resources: dict) -> None:
    """Promote has CRUD DynamoDB access and get/put/delete on S3."""
    func = _function_by_handler(resources, _HANDLER_PROMOTE)
    assert _has_dynamodb_policy(func, "DynamoDBCrudPolicy")
    assert _s3_actions_on_snapshots(func) == {
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
    }


def test_download_function_policies(resources: dict) -> None:
    """Download has a read-only DynamoDB policy and s3:GetObject only."""
    func = _function_by_handler(resources, _HANDLER_DOWNLOAD)
    assert _has_dynamodb_policy(func, "DynamoDBReadPolicy")
    assert _s3_actions_on_snapshots(func) == {"s3:GetObject"}


# ---------------------------------------------------------------------------
# Out-of-hours Lambda functions — Cognito default authorizer inheritance
# ---------------------------------------------------------------------------


def test_api_declares_cognito_default_authorizer(resources: dict) -> None:
    """The REST API declares CognitoAuthorizer as its default authorizer."""
    api = None
    for res in resources.values():
        if res.get("Type") == "AWS::Serverless::Api":
            api = res
            break
    assert api is not None, "No AWS::Serverless::Api resource found"
    auth = api["Properties"]["Auth"]
    assert auth["DefaultAuthorizer"] == "CognitoAuthorizer"
    assert "CognitoAuthorizer" in auth["Authorizers"]


def test_out_of_hours_functions_inherit_cognito_authorizer(resources: dict) -> None:
    """None of the out-of-hours routes opt out of the Cognito default authorizer.

    Unlike the ingest route (which sets ``Auth.Authorizer: NONE``), these
    routes declare no per-event authorizer override, so they inherit the API's
    CognitoAuthorizer default.
    """
    for handler in (_HANDLER_REVIEW, _HANDLER_PROMOTE, _HANDLER_DOWNLOAD):
        func = _function_by_handler(resources, handler)
        for props in _api_events(func):
            auth = props.get("Auth")
            if auth is not None:
                # If an Auth block exists it must not disable authorization.
                assert auth.get("Authorizer") not in ("NONE", None)
