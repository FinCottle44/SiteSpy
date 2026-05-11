# SiteSpy

Multi-tenant construction progress monitoring. Pole-mounted Axis cameras push periodic snapshots to AWS S3 over Starlink; stakeholders log into a web dashboard to browse snapshots, generate timelapses, and triage camera health.

## Scope

This repository contains the **backend only** — API, infrastructure, and data layer. The frontend dashboard is developed separately by an external developer.

The `docs/` folder is maintained as a living handover reference for the frontend developer, containing everything they need to integrate with the SiteSpy API without needing access to backend source code.

## Status

**Phase 0 — MVP (in development).** See [`requirements/roadmap.md`](requirements/roadmap.md) for the full phasing plan.

## Repository Layout

```
src/               Lambda handlers and shared modules (Python 3.12)
requirements/      Product & engineering specs (source of truth)
docs/              Frontend developer handover documentation (kept up to date)
scripts/           Seed, teardown, IAM policies, dev utilities
template.yaml      SAM infrastructure-as-code
samconfig.toml     Per-environment deploy configuration
```

## Documentation

### Internal (architecture & requirements)

- [`requirements/project_context.md`](requirements/project_context.md) — high-level overview
- [`requirements/roadmap.md`](requirements/roadmap.md) — phases and feature commitments
- [`requirements/api_contract.md`](requirements/api_contract.md) — all HTTP endpoints
- [`requirements/multi-tenant-auth.md`](requirements/multi-tenant-auth.md) — roles, Cognito, GDPR
- [`requirements/software_logic.md`](requirements/software_logic.md) — backend, observability, CI/CD
- [`requirements/dashboard.md`](requirements/dashboard.md) — frontend behavior spec
- [`requirements/design_system.md`](requirements/design_system.md) — visual language (liquid glass, Inter)
- [`requirements/physical_setup.md`](requirements/physical_setup.md) — hardware install guide

### Frontend Developer Docs (`docs/`)

These are maintained and kept current whenever the API changes:

- [`docs/project_description.md`](docs/project_description.md) — what SiteSpy is, architecture overview
- [`docs/api_handover.md`](docs/api_handover.md) — complete API reference for frontend integration
- [`docs/auth_handover.md`](docs/auth_handover.md) — Cognito setup, Amplify integration, role system
- [`docs/frontend_wiring_guide.md`](docs/frontend_wiring_guide.md) — credentials, endpoints, sample payloads
- [`docs/design_brief.md`](docs/design_brief.md) — visual direction, components, UX requirements
- [`docs/data_model.md`](docs/data_model.md) — DynamoDB schema and S3 key structure
- [`docs/developer_quickstart.md`](docs/developer_quickstart.md) — get running quickly

## Maintaining Frontend Docs

**Rule:** whenever a backend change affects the API surface (new endpoint, changed response shape, new auth behaviour, updated credentials), the corresponding file in `docs/` must be updated in the same commit. The frontend developer relies on `docs/` as their single source of truth.

## Getting Started (Backend)

Prerequisites:

- AWS CLI configured with a `sitespy-dev` profile
- Python 3.12
- AWS SAM CLI

```bash
sam build
sam deploy --config-env dev
```

Region: `eu-west-2` (London) for UK data residency.

## License

Proprietary. All rights reserved.
