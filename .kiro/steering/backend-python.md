---
inclusion: fileMatch
fileMatchPattern: 'src/**/*.py'
---

# Backend Python Guidance

When editing anything under `src/sitespy/`:

## Handler Shape

Every Lambda handler follows the same outer structure:

```python
@logger.inject_lambda_context(correlation_id_path='headers."X-Correlation-Id"')
@tracer.capture_lambda_handler
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event, _context):
    try:
        return _handle(event)
    except ApiError as exc:
        logger.warning(...)
        metrics.add_metric(...)
        return error_response(exc)
    except Exception:
        logger.exception("Unhandled error")
        metrics.add_metric(...)
        return unhandled_error_response(Exception())
```

The inner `_handle` is where business logic lives. The outer wrapper handles error mapping, structured logging, and metrics. Don't catch `Exception` inside `_handle` — let it bubble.

## Dependencies

- **Runtime deps:** `src/requirements.txt`. Minimal set — `aws-lambda-powertools`, `boto3`, `pydantic`. Justify any addition.
- **Dev deps:** `tests/requirements-dev.txt`.
- Never import Lambda-only libraries into `scripts/`.

## boto3 Clients

- Cache clients with `@lru_cache` at module level to survive cold starts. See `camera_auth._secrets_client` for the pattern.
- Always pass `region_name=get_settings().aws_region` when constructing clients — don't rely on ambient region.

## Error Handling

- Raise `ApiError` subclasses (`BadRequest`, `Unauthorized`, `NotFound`, `Conflict`) for all user-visible failures.
- Never return HTTP error shapes directly from business logic. The decorator handles that.
- Never leak internals. `InternalError` has a fixed generic message; put details in logs.

## Logging

- Use the `Powertools` logger. Never `print` or `logging.getLogger`.
- Log success and failure events with structured `extra` fields — `tenant_id`, `site_id`, `camera_id`, `size_bytes`, etc.
- Never log secrets, raw auth headers, or the full request body. Log the decision and the size.

## Validation

- Validate inputs early. Use `pydantic` models for JSON bodies, explicit string checks for headers and query params.
- Tenant/site/camera IDs: treat as untrusted until validated. A strict regex check belongs near the top of the handler.

## DynamoDB

- All key construction goes through `data.py` builders. Never inline `f"TENANT#{...}"` in a handler.
- Use conditional writes for idempotency and duplicate-suppression — see flag write patterns in `requirements/api_contract.md`.

## S3

- All S3 interaction goes through `storage.py`. If a handler needs a presigned URL, add a helper to `storage.py`, don't `boto3.client("s3")` in the handler.
- Always tag objects with `tenant_id` and `retention_years` for lifecycle policy routing.

## Tests

- One test module per source module.
- Use `moto` for AWS mocking (already in dev deps).
- Fixture environment variables early in conftest so `get_settings` is deterministic.
- Cover: happy path, auth failure, validation failure, not-found, idempotency where relevant.
