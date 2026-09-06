# NervOS Development Guide

## First rule

Read `docs/implementation-status.md` before coding. Do not assume target-architecture components already exist.

## Stage A prerequisites

- Git
- Python 3.12+
- uv
- Node.js supported LTS
- pnpm
- modern browser
- Playwright browser dependencies when E2E is introduced

## Target commands

```bash
make bootstrap
make dev-api
make dev-web
make test
make lint
make typecheck
make check
```

Stage A creates these progressively.

## Backend conventions

- FastAPI
- Pydantic v2 / pydantic-settings
- SQLAlchemy 2.x
- Alembic
- SQLite
- pytest
- Ruff
- Pyright

External input is validated with Pydantic. Route handlers remain thin. Core behavior belongs in services/domain modules.

## Frontend conventions

- React
- TypeScript strict mode
- Vite
- React Router
- TanStack Query
- Tailwind CSS
- Vitest + Testing Library
- Playwright E2E

Use one shared API client. Do not store authentication credentials in localStorage/sessionStorage.

## Database workflow

For every schema change:

1. update SQLAlchemy models
2. create Alembic migration
3. inspect migration manually
4. test upgrade from an empty database
5. test downgrade where supported
6. run affected tests

Never manually mutate the development/production schema as the source of truth.

## Configuration

Use typed application settings. Repository documentation contains `.env.example` with fake values only. Real `.env` files are local and should be protected from Claude Code/project tools unless explicitly needed.

## Documentation workflow

After a milestone:

- update `implementation-status.md`
- update behavior docs if behavior changed
- add an ADR for architecture decisions
- never describe future target features as already implemented

## Testing philosophy

Use many fast unit tests, integration tests at real boundaries, and a small number of E2E tests for critical user journeys.

Stage A critical E2E:

```text
fresh install -> create admin -> dashboard -> logout -> login -> refresh -> dashboard -> logout
```

## Review workflow

Use the architecture reviewer for meaningful boundary changes, the security reviewer for authentication/secrets/permissions/tool/file execution, and the test reviewer before declaring milestone completion.
