# SiteSpy — Project Structure

```
sitespy/                          # Codebase root; you work from here.
├── template.yaml                 # SAM infrastructure definition
├── samconfig.toml                # Per-env deploy params (dev / prod)
├── pyproject.toml                # Python project metadata, ruff, mypy, pytest config
├── .python-version               # 3.12
├── README.md
├── .gitignore
│
├── requirements/                 # Specs — the source of truth. Edit these when behavior changes.
│   ├── project_context.md
│   ├── roadmap.md
│   ├── api_contract.md
│   ├── multi-tenant-auth.md
│   ├── software_logic.md
│   ├── dashboard.md
│   ├── design_system.md
│   └── physical_setup.md
│
├── src/                          # Lambda source (packaged by SAM)
│   ├── requirements.txt          # Runtime deps
│   └── sitespy/                  # Single installable package
│       ├── __init__.py
│       ├── config.py             # Env var loading, validated via `get_settings()`
│       ├── errors.py             # Canonical ApiError hierarchy (BadRequest, Unauthorized, ...)
│       ├── http.py               # json_response / error_response helpers
│       ├── camera_auth.py        # Per-camera Basic Auth validation
│       ├── data.py               # DynamoDB helpers (key builders, put/get)
│       ├── storage.py            # S3 upload + canonical key builder
│       └── handlers/             # Lambda entry points
│           ├── __init__.py
│           └── ingest.py         # POST /v1/ingest
│
├── tests/                        # pytest suite; mirrors src layout
│   └── requirements-dev.txt
│
├── scripts/                      # Ops scripts (not part of the deployed Lambda)
│   ├── iam/
│   │   └── deploy-policy.json    # Minimum IAM for `sam deploy`
│   ├── seed.py                   # (tbd) Tenant/user/site/camera bootstrap
│   └── teardown.py               # (tbd) Non-prod teardown
│
└── .kiro/
    └── steering/                 # You're reading one of these right now.
```

## Conventions

- **One package.** All backend code lives under `src/sitespy/`. Shared helpers (auth, data, http, errors, storage) sit at the top level. Lambda entry points go in `handlers/`.
- **No circular imports.** Handlers depend on helpers; helpers never import handlers.
- **Config via `config.get_settings()`.** Never read `os.environ` directly from business logic. Keeps tests deterministic.
- **Errors as exceptions.** Handlers raise `ApiError` subclasses; a single try/except at the top of the handler serializes them via `http.error_response`. No ad-hoc `return {"statusCode": 400, ...}` scattered through the codebase.
- **Idempotent builders.** Functions like `build_snapshot_key` are pure — given the same inputs, produce the same output. Easier to test and reason about.
- **Tests mirror source.** `tests/sitespy/handlers/test_ingest.py` exercises `src/sitespy/handlers/ingest.py`. Same layout, same names.

## File naming

- Python: `snake_case.py`.
- TypeScript: `PascalCase.tsx` for components, `camelCase.ts` for utilities and hooks.
- Markdown specs: `kebab-case.md` (existing convention).

## Where to put new work

| New thing | Put it here |
| :--- | :--- |
| A new HTTP endpoint | New file in `src/sitespy/handlers/`, new route in `template.yaml`, test in `tests/sitespy/handlers/` |
| A new DynamoDB access pattern | Extend `src/sitespy/data.py`, never embed DB calls inside a handler |
| A new shared concern (e.g., tenant lookup, Cognito claims parsing) | New top-level module in `src/sitespy/`, used by handlers |
| An ops one-off | `scripts/` with a clear `--help` and no dependency on Lambda-only libraries |
| A new frontend screen | `frontend/src/routes/...` (once frontend scaffolding lands) |

## Where NOT to put new work

- Not in `requirements/` — that's spec, not code.
- Not in a new top-level folder unless there's an architectural reason. We keep the tree narrow.
