# SiteSpy — Product Context

SiteSpy is a multi-tenant construction progress monitoring platform. Pole-mounted Axis cameras on construction sites push periodic snapshots to AWS S3 over Starlink; stakeholders log in to a web dashboard to browse snapshots, generate timelapses, and triage camera health.

## Scope — Backend Only

**This repository is the backend only.** The frontend dashboard is developed separately by an external developer. Our responsibility is:

1. The AWS infrastructure (SAM, Lambda, DynamoDB, S3, Cognito, API Gateway)
2. All API endpoints and business logic
3. Maintaining up-to-date documentation in `docs/` for the frontend developer

**The `docs/` folder is a living handover reference.** Whenever the API surface changes (new endpoint, changed response shape, new auth behaviour, updated credentials), the corresponding file in `docs/` must be updated in the same commit. The frontend developer relies on `docs/` as their single source of truth for integrating with the SiteSpy API.

## Current Phase

**Phase 0 — MVP (in build).** End goal: one real tenant with a multi-camera site ingesting snapshots, viewing the dashboard, and having raised and resolved a flag. See `requirements/roadmap.md` for the full phase plan.

## Source of Truth

The `requirements/` folder is canonical. When making product decisions, reference these first:

- `requirements/project_context.md` — high-level overview
- `requirements/roadmap.md` — phasing, feature commitments, deferred items
- `requirements/api_contract.md` — every HTTP endpoint
- `requirements/multi-tenant-auth.md` — roles, Cognito, GDPR, data residency
- `requirements/software_logic.md` — backend design, observability, CI/CD, deployment
- `requirements/dashboard.md` — frontend behavior spec (for the external dev)
- `requirements/design_system.md` — visual language (for the external dev)
- `requirements/physical_setup.md` — hardware install guide

**Rule:** when adding or changing a feature, update the relevant requirements doc first (or in the same change). The docs must stay truthful to the code.

## Frontend Developer Documentation (`docs/`)

Maintained files:

- `docs/project_description.md` — what SiteSpy is, architecture overview
- `docs/api_handover.md` — complete API reference for frontend integration
- `docs/auth_handover.md` — Cognito setup, Amplify integration, role system
- `docs/frontend_wiring_guide.md` — live credentials, endpoints, sample payloads
- `docs/design_brief.md` — visual direction, components, UX requirements
- `docs/data_model.md` — DynamoDB schema and S3 key structure
- `docs/developer_quickstart.md` — get running quickly

**Rule:** when any API change lands, update the relevant `docs/` file so the frontend developer always has accurate information.

## Role Model

Three roles, enforced via Cognito groups:

- **Super Admin** (`SuperAdmins` group) — operates the whole platform, sees every tenant. Triages cross-tenant camera health. No `tenant_id`.
- **Tenant Admin** (`TenantAdmins` group) — operates one construction company. Sees every site in their tenant, manages users, reviews flags.
- **User** (no group) — assigned to specific sites via `custom:site_access`. Views snapshots, raises flags, generates timelapses for assigned sites.

## Tenancy Model

Tenants → Sites → Cameras. A site may have multiple cameras, each with its own `camera_id` and independent snapshot stream. Access is always per-site, never per-camera.

## Data Residency

AWS region `eu-west-2` (London). UK GDPR applies. Default image retention is 5 years, tenant-configurable 1–10.
