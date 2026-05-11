---
inclusion: manual
---

# Frontend Documentation Maintenance

The frontend dashboard is developed by an external developer. This repository does NOT contain frontend code.

## Our Responsibility

When making backend changes that affect the API surface, update the corresponding file in `docs/`:

| Change type | Update |
|---|---|
| New endpoint | `docs/api_handover.md` + sample payload in `docs/frontend_wiring_guide.md` |
| Changed response shape | `docs/api_handover.md` + `docs/frontend_wiring_guide.md` |
| New auth behaviour | `docs/auth_handover.md` |
| New credentials / env vars | `docs/frontend_wiring_guide.md` |
| Schema change (DynamoDB/S3) | `docs/data_model.md` |
| New feature affecting UX | `docs/design_brief.md` |

## Docs Inventory

- `docs/project_description.md` — what SiteSpy is, architecture overview
- `docs/api_handover.md` — complete API reference
- `docs/auth_handover.md` — Cognito setup, role system, Amplify integration
- `docs/frontend_wiring_guide.md` — live credentials, endpoints, sample payloads
- `docs/design_brief.md` — visual direction, components, UX requirements
- `docs/data_model.md` — DynamoDB schema and S3 key structure
- `docs/developer_quickstart.md` — get running quickly

## Quality Bar

The frontend developer should be able to build a fully functional dashboard using ONLY the `docs/` folder — no access to backend source code, no Slack questions about "what does this endpoint return?". If they have to ask, the docs are incomplete.
