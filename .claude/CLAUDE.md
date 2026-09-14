# NervOS — Claude Code Project Instructions

## Project

NervOS is a self-hosted AI-agent runtime and management platform.

It allows users to install, configure, schedule, run, monitor, and remove
AI agents on their own hardware.

NervOS is NOT itself an AI model and is NOT one specific agent.

Its responsibilities eventually include:

- agent runtime
- agent lifecycle management
- persistent jobs and worker execution
- scheduling and event triggers
- sessions
- scoped memory
- model-provider routing
- MCP tool routing
- permissions
- secrets
- agent package installation
- dashboard
- developer SDK
- marketplace integration

## Product principle

NervOS should make running an AI agent feel like installing an application.

Developers build agents.
NervOS installs and runs them.
Users configure and control them.

## Current development phase

`docs/implementation-status.md` is the authoritative statement of what is actually
implemented today. `docs/roadmap.md` and `docs/architecture.md` describe reviewed
direction and target boundaries, not delivered behavior. Read the status document
before starting work, and update it only after that milestone's acceptance criteria
actually pass.

Do not implement any feature belonging to a future stage or milestone without explicit
authorization for that milestone. A capability absent from the current status document
is not implemented, regardless of how the target architecture describes it.

Capabilities that remain outside every authorized milestone so far, and are therefore
still unimplemented:

- agent runtime, durable jobs, workers, queues, scheduling, and event triggers
- MCP gateway and tool execution
- scoped memory and conversation sessions
- marketplace and agent package installation
- multi-agent execution, IoT, and multi-user management
- persistent secret management

## Architecture

Use a modular monolith first.

Major boundaries:

- `apps/api` — HTTP/control-plane interface
- `apps/web` — local NervOS dashboard
- `apps/worker` — execution worker entrypoint later
- `packages/nervos-core` — core domain/application logic
- `packages/nervos-sdk` — third-party Agent SDK later
- `packages/nervos-mcp` — MCP integration later
- `packages/nervos-models` — concrete model-provider adapters (currently `anthropic` and `openai`)

The API layer may depend on nervos-core.

nervos-core MUST NOT depend on nervos-api or the React frontend.

Do not put business logic directly inside FastAPI route handlers.

## Backend stack

Use:

- Python 3.12+
- uv
- FastAPI
- Pydantic v2 / pydantic-settings
- SQLAlchemy 2.x
- Alembic
- SQLite initially
- pytest
- Ruff
- Pyright

Prefer async APIs for network/external-service operations.

## Frontend stack

Use:

- React
- TypeScript
- Vite
- React Router
- TanStack Query
- Tailwind CSS
- Vitest
- Testing Library
- Playwright for E2E testing

Use pnpm for JavaScript package management.

## Database

SQLite is the initial local database.

All schema changes must use Alembic migrations.

Do not modify database schema manually.

Never access the database directly from React.

## Security

Follow `.claude/rules/security.md`.

Never commit, log, print, or hard-code secrets.

Never read `.env` unless the user explicitly asks.

Use server-side opaque authentication sessions rather than storing auth
credentials in browser localStorage.

## Development rules

Before modifying code:

1. inspect relevant files
2. understand existing patterns
3. state the implementation plan for non-trivial changes
4. make the smallest coherent change
5. add or update tests
6. run relevant checks
7. report what changed and what remains

Do not create abstractions with no current use.

Do not silently change architecture.

Record significant architecture changes as ADRs in `docs/adr/`.

## Quality

A change is not complete until relevant:

- tests pass
- type checks pass
- lint checks pass
- database migrations work
- documentation is updated where behavior changed

Never hide failing tests.

Never delete a failing test merely to make CI pass.

## Git safety

Never force-push.

Never rewrite user history.

Never delete unrelated user changes.

Do not commit unless explicitly requested.

## Commands

Prefer repository commands once available:

- `make bootstrap`
- `make dev-api`
- `make dev-web`
- `make test`
- `make test-e2e`
- `make lint`
- `make typecheck`
- `make security`
- `make check`
- `make clean-check`

If a command does not exist yet, create it only when part of the current phase.

## Implementation status

Read `docs/implementation-status.md` before starting major work.

Update it after completing a milestone.

Do not mark a milestone complete until its acceptance criteria actually pass.