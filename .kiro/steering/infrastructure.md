---
inclusion: fileMatch
fileMatchPattern: '{template.yaml,samconfig.toml,scripts/iam/**/*.json}'
---

# Infrastructure Guidance

When editing `template.yaml`, `samconfig.toml`, or IAM policies:

## SAM Template Conventions

- **One stack per environment.** Don't create nested stacks unless you have a concrete reason.
- **Globals** for shared Lambda settings (runtime, memory, tracing, tags). Override per-function only when needed.
- **`DeletionPolicy: Retain` + `UpdateReplacePolicy: Retain`** on S3 buckets and DynamoDB tables. Protects against accidental stack deletion wiping data.
- **Tag everything** with `Project=SiteSpy` and `Environment=<env>`. Set via `Globals.Function.Tags` and resource-level `Tags`.

## Resource Naming

- Include `${Environment}` in every user-visible resource name: `sitespy-${Environment}-data`, `sitespy-${Environment}-snapshots-${AWS::AccountId}`.
- Outputs use kebab-case export names: `sitespy-${Environment}-api-endpoint`.

## IAM

- **Principle of least privilege.** Use SAM policy templates (`S3WritePolicy`, `DynamoDBCrudPolicy`) before hand-rolling.
- Resource-scope Secrets Manager access to `sitespy/${Environment}/cameras/*` only. Never `*`.
- Lambda execution roles inherit logging permissions automatically — don't re-grant `logs:*`.

## API Gateway

- REST (not HTTP API) — we need binary media type support for `image/jpeg`.
- `/v1/` prefix from day one. All new routes go under it.
- `BinaryMediaTypes` list includes `image/jpeg`, `image/png`, and `*/*`. The latter is broad but matches what Axis sends.
- Ingest route has `Authorizer: NONE` — basic auth is validated inside the Lambda, not at Gateway, because Gateway's built-in Basic support doesn't let us bind credentials per-camera.

## DynamoDB

- Single-table design. `PK` / `SK` composites documented in `src/sitespy/data.py`.
- Pay-per-request billing for Phase 0. Revisit when we have sustained traffic.
- Point-in-time recovery on. Always.
- SSE enabled (AWS-owned key is fine for now).
- `GSI1` for cross-partition queries (e.g., super-admin flag list across all tenants).

## S3

- Block all public access. Always.
- AES256 server-side encryption. Always.
- Versioning on — protects against overwrite bugs and feeds Dispute Mode integrity checks.
- Lifecycle: Standard → Glacier IR at 365 days. Expiration driven by per-object tags (`retention_years`) set at ingest.
- CORS allows `GET` from `*` because dashboards access presigned URLs from the browser. No write access via CORS.

## `samconfig.toml`

- Separate sections for `dev` and `prod` even when both point at the same AWS account.
- `confirm_changeset = true` so no deploy proceeds without a visible diff.
- `resolve_s3 = true` lets SAM manage its own artifact bucket; don't hand-create one.
- `parameter_overrides` passes the `Environment` parameter — keep this in lockstep with the `[env].global.parameters.stack_name`.

## Deploy Flow

1. **`sam build`** — verifies everything compiles and packages correctly. Safe to run anytime.
2. **`sam deploy --config-env dev`** — Kiro must ask Fin first. SAM shows the changeset; Fin reviews.
3. **`sam deploy --config-env prod`** — additional caution. Changes here affect `sitespy.io` once the business account cutover has happened.

## Prohibited Without Explicit Approval

- `sam delete`
- `aws s3 rm --recursive`
- Removing `DeletionPolicy: Retain`
- Widening IAM beyond SAM policy templates
- Disabling point-in-time recovery
- Disabling S3 versioning

## IAM Deploy Policy

`scripts/iam/deploy-policy.json` is the minimum required for `sam deploy` to succeed. Additions require:

1. A specific SAM resource that fails without the permission.
2. The narrowest ARN scope possible (prefer `sitespy-*` over `*`).
3. Update the policy + document why in the commit message Fin writes.

## Environments

- Parameters should differ via `samconfig.toml` `parameter_overrides`, never via hardcoded env-specific logic in `template.yaml`.
- Secrets and credentials are never parameters. They go in Secrets Manager or Parameter Store, referenced by ARN.
