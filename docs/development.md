# NervOS Development Guide

## First rule

Read `docs/implementation-status.md` before coding. Do not assume target-architecture components already exist, and implement only the approved milestone.

## Stage A prerequisites

- Git
- Python 3.12+
- uv
- Node.js 22.12–24.x; Node 24 LTS is the planned CI target
- pnpm 10.x; the root manifest pins pnpm 10.28.1
- GNU Make only for the optional Make facade
- a modern browser when the dashboard is implemented
- Playwright browser dependencies when E2E is introduced in A5

Repository scripts do not install global or system packages.

## A1 workspace

The uv workspace has explicit members so future placeholders are not activated accidentally:

- `apps/api`
- `packages/nervos-core`

The pnpm workspace contains only:

- `apps/web`

The worker, marketplace, SDK, MCP, and model-provider directories are future placeholders outside the active workspaces.

## Bootstrap

A clean checkout with committed lockfiles installs project-local dependencies with:

```bash
python scripts/bootstrap.py
```

Windows launcher form:

```powershell
py -3.12 scripts\bootstrap.py
```

Equivalent direct commands:

```bash
uv sync --frozen --all-packages
pnpm install --frozen-lockfile
```

Bootstrap does not install Playwright browsers, create/migrate a database, or start services.

## Repository commands

```bash
make bootstrap
make dev-api
make dev-web
make test
make lint
make typecheck
make check
```

At A2, lint, typecheck, backend/database tests, and `dev-api` are implemented. `dev-api` migrates the configured SQLite database before starting Uvicorn; migration failure prevents server startup. `dev-web` remains reserved until A4. The production frontend build, secret scan, and Playwright E2E are added to the aggregate check only in their owning milestones.

When Make is unavailable, use:

```bash
uv run python scripts/dev.py api
uv run python scripts/dev.py web
uv run python scripts/check.py test
uv run python scripts/check.py lint
uv run python scripts/check.py typecheck
uv run python scripts/check.py check
```

## Backend conventions

- Python 3.12+
- FastAPI
- Pydantic v2 / pydantic-settings
- SQLAlchemy 2.x
- Alembic
- SQLite
- pytest
- Ruff
- Pyright strict mode

A2 provides typed process configuration, synchronous SQLite/SQLAlchemy infrastructure, Alembic migrations, minimal FastAPI composition, and public process health. External input is validated with Pydantic, route handlers remain thin, and future business behavior belongs in services/domain modules.

## Frontend conventions

- React
- TypeScript strict mode
- Vite
- React Router
- TanStack Query
- Tailwind CSS
- Vitest + Testing Library
- Playwright E2E in A5

A1 establishes dependencies and tooling only. A4 adds routes, a shared API client, and UI behavior. Authentication credentials must never be stored in localStorage or sessionStorage.

## Configuration

A2 loads validated settings from process environment only. `.env.example` contains fake documentation values and is not loaded automatically. Real `.env` files are local, ignored, and protected from project tools. See `docs/configuration.md` for variables and validation.

## Database workflow (from A2)

For every schema change:

1. update SQLAlchemy models
2. create an Alembic migration
3. inspect migration operations manually
4. test upgrade from an empty temporary database
5. test downgrade where supported
6. run affected checks

Never mutate the development/production schema manually as the source of truth. Alembic is the sole schema authority; do not call `Base.metadata.create_all()`.

Use a unique file-backed database for migration/database tests, set it with `NERVOS_DATABASE_PATH`, and dispose SQLAlchemy engines before Windows cleanup. Every connection enables foreign keys and a 5000 ms busy timeout; A2 does not enable WAL. Timestamps pass through `UTCDateTime`, which rejects naive writes and restores UTC-aware values. See `docs/database.md` for the schema and migration commands.

## A3 authentication development

First-run setup must be performed while the empty installation is reachable only through loopback or another trusted interface. Every unsafe `/api/v1` request requires an `Origin` header exactly equal to `NERVOS_APP_ORIGIN`; this includes manual API calls to setup, login, and logout. Setup and login are JSON-only and bounded before parsing. Process-local Argon2 concurrency returns a recoverable 429 under saturation but does not replace reverse-proxy/network rate limiting beyond loopback. A3 adds no CORS or frontend UI.

Authentication/database tests always use Alembic-migrated, file-backed temporary databases. The setup concurrency test uses independent SQLite connections and proves that `BEGIN IMMEDIATE` permits exactly one initial administrator. See `docs/authentication.md` for the complete contract.

## Documentation workflow

After a milestone:

- update `docs/implementation-status.md` only after verification passes
- update behavior docs when behavior changes
- add an ADR for significant architecture decisions
- never describe future target features as already implemented

## Testing philosophy

Use many fast unit tests, integration tests at real boundaries, and a small number of E2E tests for critical journeys. Tests must be deterministic, order-independent, and isolated from developer data.

Stage A's future A5 E2E journey is:

```text
fresh install -> create admin user -> dashboard -> logout -> login -> refresh -> dashboard -> logout
```

## Review workflow

Review each milestone before beginning the next. Use the architecture reviewer for boundary changes, the security reviewer for authentication/secrets/permissions/tool/file execution, and the test reviewer before declaring milestone completion.
