# SiteSpy — Tech Stack

## Backend

- **IaC:** AWS SAM (`template.yaml`). Single stack per environment.
- **Runtime:** Python 3.12, ARM64 Lambdas.
- **API:** API Gateway REST, all routes under `/v1/`.
- **Storage:** S3 (snapshots) + DynamoDB (single-table: site mapping, flags, image integrity records).
- **Identity:** Cognito User Pool with groups `SuperAdmins` / `TenantAdmins`.
- **Secrets:** AWS Secrets Manager for per-camera ingest credentials and the Slack webhook URL.
- **Notifications:** Slack Incoming Webhook (flags), SES for email (invites, future scheduled reports).
- **Logging:** `aws-lambda-powertools` structured JSON with correlation IDs.
- **Tracing:** X-Ray, 10% sample in prod, 100% in dev.
- **Python deps:** declared in `src/requirements.txt`. Dev deps in `tests/requirements-dev.txt`.
- **Lint / types:** ruff + mypy strict.
- **Tests:** pytest + moto. 80% coverage target on backend.

## Frontend

- **Framework:** React 18 + TypeScript, Vite build.
- **Auth:** AWS Amplify against Cognito.
- **Styling:** Tailwind CSS + CSS variables for design tokens.
- **Component base:** shadcn/ui (Radix primitives, restyled to SiteSpy tokens).
- **Typeface:** Inter variable, self-hosted via `@fontsource-variable/inter`.
- **Icons:** Lucide React, 20px default.
- **HTTP:** Axios, Bearer token from Amplify session, `X-Correlation-Id` per request.
- **Hosting:** AWS Amplify Hosting, `eu-west-2`.
- **Tests:** Vitest + React Testing Library + one Playwright happy-path e2e per role.

## Environments & Accounts

| Env | Stack | Domain | AWS Account |
| :--- | :--- | :--- | :--- |
| dev | `sitespy-dev` | `dev.sitespy.io` | Consultancy account (for now) |
| prod | `sitespy-prod` | `sitespy.io` | Consultancy account (pre-tenants), business account (from first real tenant onward) |

**Rule:** no real paying tenant is onboarded into `sitespy-prod` while it runs in the consultancy account. Internal test tenants only. Cutover plan is in `requirements/software_logic.md` §13.1.1.

## AWS CLI Profile

`sitespy-dev` profile, region `eu-west-2`. Verified with `aws sts get-caller-identity --profile sitespy-dev`.

## CI/CD

- GitHub Actions.
- PR: lint + type-check + tests + SAM build + preview diff.
- Merge to `main` → deploy to dev automatically.
- Push to `release/*` → deploy to prod with manual approval.
- Frontend: Amplify Hosting builds from the same repo.
