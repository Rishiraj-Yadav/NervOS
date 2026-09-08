# NervOS

NervOS is a self-hosted AI-agent runtime and management platform. The repository is currently in **Stage A — Foundation**.

The A1 repository/tooling foundation and A2 API/configuration/database foundation are implemented. The public health endpoint and migration-first API launcher are available. Authentication, dashboard behavior, E2E flow, and completed CI gates are later Stage A milestones. Agent runtime, model providers, MCP/tools, workers, scheduling, memory, marketplace, IoT, and multi-agent execution are outside Stage A.

See [implementation status](docs/implementation-status.md) for the verified current state and [architecture](docs/architecture.md) for target boundaries.

## Prerequisites

- Git
- Python 3.12 or newer (the workspace is pinned to Python 3.12)
- [uv](https://docs.astral.sh/uv/)
- Node.js 22.12–24.x (Node 24 LTS is the CI target once CI exists)
- pnpm 10.x (the repository pins pnpm 10.28.1)
- GNU Make only if using the optional Make facade

The bootstrap script never installs global or system packages.

## Bootstrap

From the repository root:

```bash
python scripts/bootstrap.py
```

On Windows, an explicit Python 3.12 launcher is also supported:

```powershell
py -3.12 scripts\bootstrap.py
```

Bootstrap installs the frozen Python and JavaScript dependency sets into project-local environments. It does not install Playwright browsers, create a NervOS database, run migrations, or start services.

## Foundation commands

```bash
make bootstrap
make lint
make typecheck
make test
make check
```

GNU Make is not required. The equivalent cross-platform commands are:

```bash
python scripts/bootstrap.py
uv run python scripts/check.py lint
uv run python scripts/check.py typecheck
uv run python scripts/check.py test
uv run python scripts/check.py check
```

Start the A2 API development server with:

```bash
uv run python scripts/dev.py api
```

The launcher validates configuration, runs Alembic `upgrade head`, and starts Uvicorn on `http://127.0.0.1:8000`. Liveness is available at `GET /api/v1/health` and returns `{"status":"ok"}`. The web command remains deferred to A4:

```bash
uv run python scripts/dev.py web
```

A3 implements backend first-run setup and local authentication at `POST /api/v1/setup`, `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, and `GET /api/v1/auth/me`. See [authentication](docs/authentication.md). The React setup/login/dashboard UI and user management remain unimplemented and are not part of A3.

## Workspace boundaries

Python uv workspace members:

- `apps/api`
- `packages/nervos-core`

pnpm workspace members:

- `apps/web`

`apps/worker`, `apps/marketplace`, `packages/nervos-sdk`, `packages/nervos-mcp`, and `packages/nervos-models` are future placeholders. They are not active workspaces and contain no Stage A behavior.

## Configuration and security

A2 reads `NERVOS_ENVIRONMENT`, `NERVOS_DATABASE_PATH`, `NERVOS_APP_ORIGIN`, and `NERVOS_LOG_LEVEL` from the process environment. It does not automatically load `.env.example` or any real `.env` file. See [configuration](docs/configuration.md) and [database foundation](docs/database.md). Real environment files, local databases, generated output, credentials, and local tool state must remain untracked.

## License

License selection is pending an owner decision. The empty `LICENSE` placeholder is not a grant of permission or a statement of redistribution terms.
