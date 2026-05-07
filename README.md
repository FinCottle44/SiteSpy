# SiteSpy

Multi-tenant construction progress monitoring. Pole-mounted Axis cameras push hourly snapshots to AWS S3 over Starlink; stakeholders log into a React dashboard to browse snapshots, generate timelapses, and triage camera health.

## Status

**Phase 0 — MVP (in development).** See [`requirements/roadmap.md`](requirements/roadmap.md) for the full phasing plan.

## Repository Layout

```
requirements/      Product & engineering specs (source of truth)
scripts/           Seed, teardown, IAM policies, dev utilities
```

## Documentation

Every architectural decision lives under `requirements/`:

- [`project_context.md`](requirements/project_context.md) — high-level overview
- [`roadmap.md`](requirements/roadmap.md) — phases and feature commitments
- [`api_contract.md`](requirements/api_contract.md) — all HTTP endpoints
- [`multi-tenant-auth.md`](requirements/multi-tenant-auth.md) — roles, Cognito, GDPR
- [`software_logic.md`](requirements/software_logic.md) — backend, observability, CI/CD
- [`dashboard.md`](requirements/dashboard.md) — frontend behavior
- [`design_system.md`](requirements/design_system.md) — visual language (liquid glass, Inter)
- [`physical_setup.md`](requirements/physical_setup.md) — hardware install guide

## Getting Started

Prerequisites will be documented as the scaffolding lands. At a minimum you'll need:

- AWS CLI configured with a `sitespy-dev` profile (see [`scripts/iam/deploy-policy.json`](scripts/iam/deploy-policy.json))
- Python 3.12
- Node 20+
- AWS SAM CLI

Region: `eu-west-2` (London) for UK data residency.

## License

Proprietary. All rights reserved.
