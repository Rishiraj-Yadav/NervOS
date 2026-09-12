# NervOS

NervOS is a self-hosted AI-agent runtime and management platform. The repository is currently in **Stage A — Foundation**.

The A1 repository/tooling foundation, A2 API/configuration/database foundation, A3 local-authentication boundary, A4 React dashboard foundation, A5 deterministic browser journey, and A6 continuous-integration/security-scanning milestone are implemented. The browser supports first-run setup, cookie-backed login/session restoration, a protected minimal dashboard, and server-confirmed logout. Agent runtime, model providers, MCP/tools, workers, scheduling, memory, marketplace, IoT, and multi-agent execution are outside Stage A.

See [implementation status](docs/implementation-status.md) for the verified current state and [architecture](docs/architecture.md) for target boundaries.

## Prerequisites

- Git
- Python 3.12 or newer (the workspace is pinned to Python 3.12)
- [uv](https://docs.astral.sh/uv/) 0.9.15 or newer
- Node.js 22.12–24.x; CI runs Node 24 (declared in `.node-version`), which is the upper bound of the supported range
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

Bootstrap installs the frozen Python and JavaScript dependency sets, then provisions the locked Playwright-managed Chromium browser. It never installs global or system packages, creates a NervOS database, runs migrations, or starts services. To install dependencies without the browser, pass `--skip-browser`; CI's browser-free job uses this so routine checks never download Chromium. To repair only the browser prerequisite, run:

```bash
pnpm --dir apps/web exec playwright install chromium
```

Do not use `--with-deps` locally; Stage A bootstrap provisions Chromium only. The CI runner installs its own Playwright system libraries, which is a runner-only concern.

## Foundation commands

```bash
make bootstrap
make dev-api
make dev-web
make lint
make typecheck
make test
make security
make test-e2e
make check
make clean-check
```

GNU Make is not required. The equivalent cross-platform commands are:

```bash
python scripts/bootstrap.py
uv run python scripts/dev.py api
uv run python scripts/dev.py web
uv run python scripts/check.py lint
uv run python scripts/check.py typecheck
uv run python scripts/check.py test
uv run python scripts/check.py security
uv run python scripts/check.py e2e
uv run python scripts/check.py check
uv run python scripts/clean_check.py
```

`check` is the routine gate and covers lint, typecheck, tests and the tracked-file security scan. It is deliberately **not** the whole story: the deterministic browser journey lives in the separate `e2e` group because it needs Chromium and spawns services. Full verification is therefore `check` then `e2e`, which is exactly what CI runs. See [continuous integration](docs/ci.md).

Start the A2 API development server with:

```bash
uv run python scripts/dev.py api
```

The API launcher validates configuration, runs Alembic `upgrade head`, and starts Uvicorn on `http://127.0.0.1:8000`. Liveness is available at `GET /api/v1/health` and returns `{"status":"ok"}`. Start the A4 web application separately with:

```bash
uv run python scripts/dev.py web
```

Open exactly `http://localhost:5173`. Vite proxies relative `/api/v1/...` requests to the local API so the browser remains same-origin and satisfies A3's exact-Origin policy. A4 consumes the A3 setup/authentication routes through one credentialed API client; authentication remains in the HttpOnly server session cookie and is restored through `/auth/me`. User management remains unimplemented.

## Browser E2E

Run the A5 journey with `uv run python scripts/check.py e2e` (or `make test-e2e` where GNU Make is available). The supervisor creates a unique temporary SQLite database, migrates it with Alembic before starting services, selects dynamic `127.0.0.1` API and Vite ports, aligns the exact browser Origin and Vite proxy, waits with bounded HTTP readiness checks, and owns the API, Vite, Playwright, and Chromium process trees. The journey uses no MSW or external service and covers setup, logout, login, reload session restoration, and protected-route redirect after final logout.

`check` deliberately remains E2E-free, because routine work must never require Chromium; full local verification is `check` then `e2e`, and CI runs both. Failure traces and screenshots are ignored under Playwright output paths. If Chromium is missing, rerun the documented Chromium-only provisioning command above. API/Vite early-exit and port-handoff diagnostics are written through the supervisor's bounded logs; do not substitute `localhost` for its `127.0.0.1` test Origin.

## Continuous integration and security scanning

`.github/workflows/ci.yml` runs on pull requests, pushes to `main`, and manually. It has two jobs, each invoking repository-owned commands only:

- **check** — frozen dependencies via `bootstrap.py --skip-browser`, then `scripts/check.py check`. No browser, no spawned services.
- **e2e** — full bootstrap including Chromium, the CI-only Playwright system libraries, then `scripts/check.py e2e`.

`.github/workflows/security.yml` runs the repository-owned tracked-file scanner on pull requests, pushes to `main`, and a weekly schedule. It needs no project dependencies because the scanner is standard-library only.

Both workflows declare `permissions: contents: read`, reference no secrets, use `pull_request` rather than `pull_request_target`, and pin every action to a verified full-length commit SHA. No external AI provider or third-party service is contacted. See [continuous integration](docs/ci.md) for the pinning policy, cache strategy, and scanner rules.

## Workspace boundaries

Python uv workspace members:

- `apps/api`
- `packages/nervos-core`

pnpm workspace members:

- `apps/web`

`apps/worker`, `apps/marketplace`, `packages/nervos-sdk`, `packages/nervos-mcp`, and `packages/nervos-models` are future placeholders. They are not active workspaces and contain no Stage A behavior.

## Configuration and security

A2 reads `NERVOS_ENVIRONMENT`, `NERVOS_DATABASE_PATH`, `NERVOS_APP_ORIGIN`, and `NERVOS_LOG_LEVEL` from the process environment. It does not automatically load `.env.example` or any real `.env` file. See [configuration](docs/configuration.md) and [database foundation](docs/database.md). Real environment files, local databases, generated output, credentials, and local tool state must remain untracked.

Production requires HTTPS: the session cookie is `Secure` whenever production mode or an HTTPS `NERVOS_APP_ORIGIN` is configured, and it is always `HttpOnly`. Initialize NervOS while it is bound to loopback or otherwise reachable only through a trusted interface. The process-local Argon2 work bound returns a recoverable `429` under saturation but is **not** a substitute for rate limiting — any deployment exposed beyond loopback must enforce its own reverse-proxy or network rate limits. See [authentication](docs/authentication.md) and [sessions](docs/sessions.md).

## License

License selection is pending an owner decision. The empty `LICENSE` placeholder is not a grant of permission or a statement of redistribution terms.
