# NervOS — Project Instructions for Codex

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

`docs/implementation-status.md` is the single authoritative statement of delivered
state for every capability: what is implemented, accepted, and current. Do not keep a
static list of "unimplemented capabilities" here — it goes stale as stages land. Before
claiming a feature exists or is missing, verify it against that document (and, for Stage F,
the Stage-F master plan) rather than guessing from target architecture.


## Architecture

Use a modular monolith first.

Major boundaries:

- `apps/api` — HTTP/control-plane interface
- `apps/web` — local NervOS dashboard
- `apps/worker` — execution worker entrypoint
- `packages/nervos-core` — core domain/application logic
- `packages/nervos-sdk` — third-party Agent SDK later
- `packages/nervos-mcp` — MCP integration
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

Follow the Security section below and the more specific nested `AGENTS.md` files.

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

## Stage F

Stage F is governed by the frozen master plan `docs/stage-f/README.md` and accepted
ADRs 0021–0023. Before any Stage-F change:

1. Read `docs/stage-f/README.md` and the accepted Stage-F ADRs.
2. Run `git hash-object docs/stage-f/README.md` and report `Stage-F plan blob: <hash>`.
3. Verify the current milestone and that its predecessor is complete.

Do not start a Stage-F milestone without explicit authorization for that milestone.
On any conflict with the frozen Stage-F architecture, stop and report
`STAGE F ARCHITECTURE CHANGE REQUEST — <issue>` rather than silently deviating from
the accepted plan.

# NervOS Architecture Rules

## Architectural style

NervOS begins as a modular monolith.

Do not introduce microservices, Kafka, Kubernetes, Redis, or distributed
infrastructure unless a later requirement justifies them.

## Core dependency direction

Allowed:

apps/api
    ↓
packages/nervos-core

apps/worker
    ↓
packages/nervos-core

packages/nervos-sdk
    ↓
public NervOS interfaces

Not allowed:

nervos-core → nervos-api
nervos-core → React frontend
domain → infrastructure implementation

## Runtime domain model

Preserve these distinctions:

AgentPackage != AgentInstance
AgentInstance != Run
Session != Run
User != Agent
Memory != Session

Future execution architecture:

Trigger
  ↓
Job
  ↓
Queue
  ↓
Worker
  ↓
Run Coordinator
  ↓
Agent Instance
  ↓
Model / Memory / Tools

Installed agents should normally be idle records.
They must not require a permanent thread/process while inactive.

## Control plane vs execution plane

Control plane:
- API
- dashboard
- auth
- configuration
- registry
- marketplace management

Execution plane:
- scheduler
- queue
- workers
- runtime
- model calls
- tool calls

Keep these concepts separated even while implemented in one repository.

## Persistence

SQLite is the v1 local persistence mechanism.

Keep repositories/services abstract enough that PostgreSQL can be supported later,
but do not build PostgreSQL-specific infrastructure before it is needed.

## Extensibility

Future agent developers should interact with stable NervOS SDK abstractions,
not NervOS internal database models.

Future MCP tools must go through the NervOS tool/permission layer.

Future model providers must go through a model-provider interface.

## Architecture changes

Any significant change to these principles requires an ADR under `docs/adr/`.

# NervOS Documentation Rules

Documentation is part of the deliverable, not an afterthought.

## Source of truth

`docs/implementation-status.md` is the source of truth for verified state.

Read it before starting major work.

Update it after an accepted milestone, and only after that milestone's
acceptance criteria actually pass.

Do not mark a milestone complete while its criteria are unverified.

## Current vs future

Never describe future or target architecture as implemented.

Clearly separate what Stage A implements today from what is planned later.

When a milestone completes, update the tense of descriptions that have become
current; do not leave stale "will" statements about delivered behavior.

## Behavior changes

Update behavior documentation in the same change as the behavior change.

A change that alters public behavior is not complete until its documentation
matches.

## Architecture decisions

Record significant architecture decisions as ADRs under `docs/adr/`.

An ADR is required when a change alters an architecture invariant or boundary,
not for routine implementation detail.

## Secrets

Never write credentials, tokens, or secret values into documentation.

`.env.example` may contain only fake placeholder values.

Do not copy real values from `.env`, logs, or configuration into any document.

## Review

Documentation is reviewed before a milestone is declared complete.

Review accuracy, not only existence: confirm statements match the verified
state recorded in `docs/implementation-status.md`.

# NervOS Security Rules

Security boundaries are part of the architecture, not optional polish.

## Secrets

Never:
- hard-code API keys
- commit API keys
- print secrets in logs
- place secrets in agent memory
- put secrets into agent package manifests
- expose secret values through API responses

Secrets must eventually be handled through the NervOS Secret Manager.

## Sensitive files

Do not read or modify:
- `.env`
- `.env.*`
- private keys
- credential files
- `secrets/`

unless the user explicitly requests it.

Use `.env.example` with fake values for documentation.

## Authentication

For the local NervOS dashboard use server-side opaque sessions.

Do not store authentication tokens in localStorage.

Session cookies must be:
- HttpOnly
- SameSite configured appropriately
- Secure when HTTPS/production is enabled

Password hashes must use Argon2id.

Raw session tokens must not be stored in the database.
Store a cryptographic hash of the session token.

Authentication error messages must not expose credentials or hashes.

## Initial setup

Admin account creation is allowed only while no account exists.

After initial setup, bootstrap/setup endpoints must not allow another
unauthenticated admin to be created.

## Authorization

Every protected backend route must establish the authenticated user.

Never trust a user_id supplied by the browser as proof of identity.

Ownership must be checked server-side.

## API

Validate all external inputs through Pydantic schemas.

Never interpolate untrusted input into SQL or shell commands.

Do not expose Python exceptions or stack traces through production API responses.

## CORS

Never use `*` together with credentialed browser authentication.

Prefer same-origin API access.

For development, use the Vite proxy where practical.

## Logging

Never log:
- passwords
- session tokens
- API keys
- authorization headers
- OAuth refresh tokens

## Future agent security

Marketplace agents must be considered untrusted.

Do not design APIs that require arbitrary agent code to receive raw credentials.

Future tool calls must pass through:
Agent → Permission Engine → MCP Gateway → Tool.

Do not allow agents to bypass the permission layer by directly accessing
host resources.

# Testing Rules

Use the testing pyramid:

unit tests
    ↓
integration tests
    ↓
small number of E2E tests

Backend:
- pytest
- test public behavior rather than implementation details
- use isolated temporary databases
- never use a developer's real NervOS database

Frontend:
- Vitest
- Testing Library
- test behavior from the user's perspective

E2E:
- Playwright
- use deterministic test users/data
- do not rely on external AI providers during Stage A

Every bug fix should include a regression test when practical.

Never make tests dependent on execution order.


