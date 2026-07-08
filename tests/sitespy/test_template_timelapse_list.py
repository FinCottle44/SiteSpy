"""Configuration / smoke tests for timelapse-job-listing infrastructure.

These example-based tests assert the declarative infrastructure in
``sitespy/template.yaml`` and its alignment with the application config:

- The ``ExpireTimelapseArtifacts`` S3 lifecycle rule targets the
  ``timelapse/`` prefix and expires objects after 30 days (Requirements 7.2, 7.3).
- The configured ``retention_days`` equals the template ``ExpirationInDays``
  and the submit function's ``JOB_TTL_DAYS`` env — a single retention value
  feeds both the submit TTL computation and the S3 lifecycle rule
  (Requirements 7.3, 7.4).
- ``TimelapseListFunction`` exists, serves ``GET /v1/timelapse-jobs`` with a
  ``DynamoDBReadPolicy`` and ``s3:GetObject`` on ``timelapse/*``, and its route
  is distinct from ``GET /v1/timelapse-jobs/{job_id}`` (TimelapseGetFunction)
  (Requirement 1.1).

The template uses CloudFormation intrinsic tags (``!Sub``, ``!Ref``,
``!GetAtt``, etc.).  A plain ``yaml.safe_load`` cannot parse these, so we
register a permissive multi-constructor for the ``!`` tag prefix that turns
each intrinsic into a simple sentinel string, letting us inspect the plain
structural values (prefixes, days, paths, methods) we care about.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from sitespy.config import get_settings

# ---------------------------------------------------------------------------
# CloudFormation-tolerant YAML loader
# ---------------------------------------------------------------------------


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation intrinsic ``!`` tags."""


def _cfn_multi_constructor(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):
    """Collapse any ``!<Intrinsic>`` tag into a plain Python value.

    Scalars become ``"!<tag> <value>"`` strings; sequences and mappings are
    constructed normally and returned as-is.  This keeps the structural,
    non-intrinsic values (prefixes, expiration days, paths, HTTP methods)
    directly inspectable while never raising on an unknown ``!`` tag.
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


def _timelapse_rule(resources: dict) -> dict:
    rules = _lifecycle_rules(resources)
    matches = [r for r in rules if r.get("Id") == "ExpireTimelapseArtifacts"]
    assert matches, "ExpireTimelapseArtifacts lifecycle rule not found"
    assert len(matches) == 1, "Expected exactly one ExpireTimelapseArtifacts rule"
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


# ---------------------------------------------------------------------------
# S3 lifecycle retention
# ---------------------------------------------------------------------------


def test_expire_timelapse_artifacts_prefix_and_days(resources: dict) -> None:
    """ExpireTimelapseArtifacts targets timelapse/ and expires after 30 days."""
    rule = _timelapse_rule(resources)
    assert rule["Prefix"] == "timelapse/"
    assert rule["ExpirationInDays"] == 30
    assert rule["Status"] == "Enabled"


# ---------------------------------------------------------------------------
# Retention alignment (single source of truth)
# ---------------------------------------------------------------------------


def test_retention_days_matches_template_expiration(resources: dict) -> None:
    """Configured retention_days equals the template ExpirationInDays (both 30)."""
    settings = get_settings()
    rule = _timelapse_rule(resources)
    assert settings.retention_days == 30
    assert settings.retention_days == rule["ExpirationInDays"]


def test_single_value_feeds_ttl_and_lifecycle(resources: dict) -> None:
    """One retention value drives the submit TTL and the S3 lifecycle rule.

    - config.retention_days drives job_ttl_days (the submit TTL computation).
    - the submit function's single retention env var feeds config.retention_days.
    - the template's timelapse expiration equals that same value.
    """
    settings = get_settings()

    # config.retention_days is the single source that drives job_ttl_days;
    # get_settings reads RETENTION_DAYS and derives job_ttl_days from it.
    assert settings.job_ttl_days == settings.retention_days

    submit_func = _function_by_handler(
        resources, "sitespy.handlers.timelapse_post.handler"
    )
    submit_env = submit_func["Properties"]["Environment"]["Variables"]
    # The submit function configures retention via a single RETENTION_DAYS env
    # var, which get_settings reads to compute both retention_days and the TTL
    # anchor (job_ttl_days) used by the submit handler.
    submit_retention_days = int(submit_env["RETENTION_DAYS"])
    lifecycle_days = _timelapse_rule(resources)["ExpirationInDays"]

    # The submit TTL input and the S3 lifecycle expiry resolve to the same value.
    assert submit_retention_days == settings.retention_days
    assert submit_retention_days == lifecycle_days


# ---------------------------------------------------------------------------
# TimelapseListFunction wiring
# ---------------------------------------------------------------------------


def test_timelapse_list_function_exists(resources: dict) -> None:
    func = _function_by_handler(resources, "sitespy.handlers.timelapse_list.handler")
    assert func["Type"] == "AWS::Serverless::Function"


def test_timelapse_list_serves_get_list_route(resources: dict) -> None:
    """TimelapseListFunction has an Api event for GET /v1/timelapse-jobs."""
    func = _function_by_handler(resources, "sitespy.handlers.timelapse_list.handler")
    routes = [(e.get("Method"), e.get("Path")) for e in _api_events(func)]
    assert ("GET", "/v1/timelapse-jobs") in routes


def test_timelapse_list_has_dynamodb_read_and_s3_get(resources: dict) -> None:
    """TimelapseListFunction has a DynamoDBReadPolicy and s3:GetObject on timelapse/*."""
    func = _function_by_handler(resources, "sitespy.handlers.timelapse_list.handler")
    policies = func["Properties"]["Policies"]

    has_read_policy = any(
        isinstance(p, dict) and "DynamoDBReadPolicy" in p for p in policies
    )
    assert has_read_policy, "TimelapseListFunction missing DynamoDBReadPolicy"

    # Collect every (action, resource) pair across inline policy statements.
    get_object_on_timelapse = False
    for policy in policies:
        if not isinstance(policy, dict) or "Statement" not in policy:
            continue
        statements = policy["Statement"]
        if isinstance(statements, dict):
            statements = [statements]
        for stmt in statements:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            resource = stmt.get("Resource", "")
            if "s3:GetObject" in actions and "timelapse/*" in resource:
                get_object_on_timelapse = True
    assert get_object_on_timelapse, (
        "TimelapseListFunction missing s3:GetObject on timelapse/*"
    )


def test_list_route_distinct_from_get_route(resources: dict) -> None:
    """GET /v1/timelapse-jobs (list) is distinct from GET /v1/timelapse-jobs/{job_id}."""
    list_func = _function_by_handler(
        resources, "sitespy.handlers.timelapse_list.handler"
    )
    get_func = _function_by_handler(
        resources, "sitespy.handlers.timelapse_get.handler"
    )

    list_routes = {(e.get("Method"), e.get("Path")) for e in _api_events(list_func)}
    get_routes = {(e.get("Method"), e.get("Path")) for e in _api_events(get_func)}

    assert ("GET", "/v1/timelapse-jobs") in list_routes
    assert ("GET", "/v1/timelapse-jobs/{job_id}") in get_routes
    # The two functions serve disjoint routes — no collision on the base path.
    assert list_routes.isdisjoint(get_routes)
    assert ("GET", "/v1/timelapse-jobs") not in get_routes
    assert ("GET", "/v1/timelapse-jobs/{job_id}") not in list_routes
