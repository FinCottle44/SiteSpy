# SiteSpy — Working Conventions

## Collaboration Model

- **Fin handles git and pushes.** Never run `git add`, `git commit`, `git push`, or any git state-changing command.
- **Kiro handles `sam build` freely.** Build is local and safe.
- **Kiro asks before `sam deploy`** — deploys touch the AWS account. Confirm in chat first.
- **Destructive AWS commands need explicit approval** — `sam delete`, `aws s3 rm --recursive`, DynamoDB table drops, anything with `--force`.
- **Default AWS profile:** `sitespy-dev`. Region `eu-west-2`.

## API Conventions

- All routes under `/v1/`. Breaking changes go to `/v2/` — non-breaking additions stay on `/v1/`.
- Snake_case for query params and JSON fields (`site_id`, `camera_id`, `tenant_id`).
- **Exception:** the Axis camera ingest uses `?cameraID=<id>` (camelCase) because that's what the VAPIX recipient URL is configured with. The ingest Lambda normalizes to `camera_id` internally.
- Canonical error envelope:
  ```json
  { "error": "ERROR_KEY", "message": "Human readable." }
  ```
- Opaque cursor pagination. `next_cursor` is base64-encoded JSON; clients treat it as opaque.
- All timestamps in ISO 8601 UTC (`2025-06-15T14:00:00Z`). No Unix timestamps anywhere.

## Timezone Display Rule

Every timestamp shown in the dashboard is rendered in the viewed **site's** timezone (from `SITE#.timezone`, default `Europe/London`), not the browser's timezone. Use the shared `<Timestamp />` component — no raw `.toLocaleString()` calls anywhere.

## Authorization

- Role is resolved from `cognito:groups` via `resolve_role(event)`. Never hand-rolled from a custom attribute.
- All authorization flows through `check_access(role, tenant_id, site_id)` — no ad-hoc claim checks in handlers.
- Ingest uses per-camera HTTP Basic Auth with credentials stored in Secrets Manager. Never share credentials across cameras.

## Data Model

- **Single-table DynamoDB.** See `src/sitespy/data.py` for key builders. Never construct PK/SK strings inline in a handler.
- **S3 key format:** `<tenant_id>/<site_id>/<camera_id>/<YYYY>/<MM>/<DD>/<YYYY-MM-DDTHH:mm:ssZ>.jpg`. Use `storage.build_snapshot_key` — never inline.
- **Every ingest writes an `IMG#` record** with SHA-256. This is a Phase 0 investment for future Dispute Mode (`requirements/roadmap.md` B.1).

## Code Style

- **Python:** ruff + mypy strict (configured in `pyproject.toml`). Run `ruff check` and `ruff format` before considering work complete.
- **Type hints everywhere.** No untyped function signatures in `src/`.
- **Small modules.** If a file passes ~300 lines it probably wants splitting.
- **Docstrings on public functions.** One-liner is fine; explain intent, not mechanics.
- **No magic values.** Retention years, timeouts, limits — give them a named constant at module top.

## Testing

- pytest + moto for AWS mocking. Integration tests use LocalStack where moto falls short.
- Mirror `src/` layout under `tests/`. One test module per source module.
- Mark LocalStack/AWS-requiring tests with `@pytest.mark.integration` so unit runs stay fast.
- Every handler needs at minimum: happy path, auth failure, validation failure, not-found, and (where relevant) idempotency.
- Never test by round-tripping through a deployed Lambda when unit-level mocks will do.

## Security Defaults

- Treat all inputs as hostile. Validate tenant_id/site_id/camera_id with strict regex.
- Use `hmac.compare_digest` for credential comparisons, never `==`.
- Never log secrets or raw Basic Auth headers. Log the decision (accepted/rejected), not the material.
- Never echo stack traces or internal error details to clients. Generic `INTERNAL_ERROR` only.
- S3 buckets default to fully private. Access exclusively via presigned URLs.

## When Requirements and Code Disagree

- If existing code contradicts `requirements/`, **the requirements doc is canonical** — fix the code.
- If you find a need to diverge from the spec, update the requirements doc first in the same change, not after.

## Communication

- Prefer short answers over long ones unless thinking aloud is genuinely helpful.
- When uncertain, ask rather than guess. A two-sentence question beats a wrong implementation.
- After any code change, run the relevant verification (`sam build`, `ruff check`, `pytest`) and report the result before handing back.
