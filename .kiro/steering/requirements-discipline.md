---
inclusion: fileMatch
fileMatchPattern: 'requirements/*.md'
---

# Requirements Documentation Discipline

When editing anything in `requirements/`:

## The Canonical Rule

`requirements/` is the source of truth. Code follows the spec, not the other way around. When behavior changes, the spec changes in the same commit as the code.

## What Belongs Here

- Product decisions: what SiteSpy does, for whom, in what order.
- Contracts: API shapes, data model, auth flow.
- Standards: conventions that every implementation must follow (timestamp format, error envelope, key structure).
- Rationale: why a choice was made, especially when it constrains future work (e.g., per-image SHA-256 in Phase 0 to enable Dispute Mode without a backfill).

## What Does NOT Belong Here

- Implementation details that don't affect contracts (choice of React hook library, internal helper layout).
- Operational runbooks (those go in a future `docs/runbooks/` folder).
- Code samples beyond what's needed to illustrate a contract.

## Cross-Linking

- Reference other docs by filename: `` `requirements/api_contract.md` ``. Readers should be able to navigate the spec by following these.
- When a concept has a canonical definition, link to it rather than re-explaining. For instance, the role matrix lives in `multi-tenant-auth.md` §1 — other docs link to it, they don't restate it.

## Tone

- Direct, technical, no marketing voice.
- Prefer bullet tables over prose for configuration and parameters.
- Include examples where a contract is non-obvious (JSON bodies, key formats).
- State what the system MUST do (capitalized MUST/MUST NOT when it's a hard requirement).

## When Adding a New Feature

1. Pick the correct doc (or argue for a new one in conversation first).
2. Add the feature definition: user need → API contract → data model impact → UX.
3. Cross-link to related docs.
4. Update `roadmap.md` if the phase or priority shifts.
5. Only then touch code.

## When Deferring a Feature

- Put it in `roadmap.md` with a clear phase assignment or "Under Review" / "Rejected" with reasoning.
- Don't let good ideas vanish into chat history.

## Versioning

- No formal versioning on the specs themselves. The git history is the change log.
- If a contract change would break live cameras or dashboards, it must land on `/v2/` — never modify `/v1/` semantics silently.
