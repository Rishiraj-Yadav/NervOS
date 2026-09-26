# NervOS Development Guide

## First rule

Read `docs/implementation-status.md` before coding. Do not assume target-architecture components already exist, and implement only the approved milestone.

## Stage A prerequisites

- Git
- Python 3.12+
- uv 0.9.15 or newer
- Node.js 22.12–24.x; CI runs Node 24, declared in `.node-version`, which is the upper bound of the supported range
- pnpm 10.x; the root manifest pins pnpm 10.28.1
- GNU Make only for the optional Make facade
- Playwright-managed Chromium for the A5 browser journey (provided by bootstrap; skipped with `--skip-browser`)

Repository scripts do not install global or system packages.

## A1 workspace

The uv workspace has explicit members so future placeholders are not activated accidentally:

- `apps/api`
- `apps/scheduler`
- `apps/worker` (activated in C2 for durable execution; extended in C3 with the registry/reconciler and in C4 with durable safe execution retry)
- `packages/nervos-core`
- `packages/nervos-mcp` (activated in D5 for MCP client/gateway and connection lifecycle)
- `packages/nervos-models` (activated in B2 for Anthropic and extended in B4 with the OpenAI Responses adapter)

The pnpm workspace contains only:

- `apps/web`

The marketplace and SDK directories remain future placeholders outside the active workspaces. `apps/worker` is a real process entrypoint but owns no HTTP surface and never runs Alembic. `packages/nervos-models` is active only for concrete provider infrastructure; `nervos-core` remains provider-SDK-free, and both provider SDKs are isolated there.

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
pnpm --dir apps/web exec playwright install chromium
```

Bootstrap provisions only the locked Playwright-managed Chromium after frozen dependencies. It does not install global/system packages, create or migrate a NervOS database, create users, or start services. The final command can be rerun independently to repair a missing browser; never add `--with-deps` because operating-system dependency installation belongs outside project bootstrap.

Pass `--skip-browser` to install the frozen Python and pnpm dependency sets without provisioning Chromium. CI's browser-free `check` job uses this path so routine checks never download a browser. The default remains browser-provisioning, so local behaviour is unchanged.

## Repository commands

```bash
make bootstrap
make dev-api
make dev-worker
make dev-web
make test
make test-e2e
make lint
make typecheck
make security
make check
make clean-check
```

Lint, typecheck, backend/database tests, the tracked-file security scan, deterministic Playwright E2E, and the development launchers are implemented. `dev-api` migrates the configured SQLite database before starting Uvicorn; migration failure prevents server startup. `dev-worker` starts the Stage C Worker against the already-migrated database and never runs Alembic. `dev-web` starts the Vite application. The production frontend build is verified separately with `pnpm build`.

`check` is the routine gate: lint, typecheck, tests, and the security scan. It deliberately excludes E2E, because routine work must never require Chromium or spawn services. **Full verification is `check` then `e2e`**, exactly as CI runs it. See [continuous integration](ci.md).

When Make is unavailable, use:

```bash
uv run python scripts/dev.py api
uv run python scripts/dev.py worker
uv run python scripts/dev.py web
uv run python scripts/check.py test
uv run python scripts/check.py security
uv run python scripts/check.py e2e
uv run python scripts/check.py lint
uv run python scripts/check.py typecheck
uv run python scripts/check.py check
uv run python scripts/clean_check.py
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

A4 implements `/`, `/setup`, `/login`, `/dashboard`, and an accessible not-found route. TanStack Query owns setup status and the current server session through the stable `setup-status` and `auth-session` queries. One shared API client uses relative `/api/v1/...` URLs with browser credentials. Authentication credentials and tokens are never stored in localStorage or sessionStorage.

For local development, start the API, Worker, and web launcher in separate terminals, in that order, then open exactly `http://localhost:5173`. The API applies migrations; the Worker validates the existing schema revision and refuses to start if it is not current; Vite proxies `/api` without rewriting it to `http://127.0.0.1:8000`. Using a different browser hostname will fail A3's exact-Origin policy. Frontend component behavior is tested with Vitest, Testing Library, and MSW.

## A5 browser E2E

Run the complete browser journey with:

```bash
uv run python scripts/check.py e2e
```

Or, where GNU Make is available:

```bash
make test-e2e
```

The Python supervisor creates a unique temporary run directory and SQLite database on every invocation, runs Alembic before starting any server, starts the API, Worker, Vite, and Playwright processes, selects distinct dynamic IPv4 loopback ports, and derives one consistent `127.0.0.1` browser Origin for FastAPI, Vite, and Playwright. Worker readiness is a test-only marker file written after settings load, schema validation, provider resolution, durable registration, and the startup reclamation pass. The supervisor also runs a second Worker for the C3 pre-start recovery journey: the first claims one Job and is lost before the execution-start boundary, and the second reconciles that expired claim and executes it exactly once. One prompt in the journey exercises the C4 retry path: the deterministic provider double refuses its first call with a normalized rate limit, the Worker commits a durable `retry_wait` with a test-only stretched delay, and the supervisor proves from committed state that a second Attempt ran only after the due instant, that the Run kept its original start, and that the prompt made exactly two provider calls. One further prompt exercises the C5 cancellation path: the double holds that provider call open until the browser cancels the Run, the supervisor proves from committed state that the Run became durably `cancelled` with its real start preserved and no provider error, and a test-only ledger proves the Worker actually stopped its blocked local call rather than merely not having answered yet. The supervisor waits out the shipped heartbeat interval for that discovery instead of shortening it, because the C3 recovery journey depends on the production lease relationship. Vite keeps `/api` relative and unrewritten with `changeOrigin: false`; the E2E-only environment override changes only its proxy target.

Readiness uses bounded semantic HTTP polling and child-liveness checks rather than startup sleeps. The supervisor owns and cleans the exact Uvicorn, Vite, Playwright, and Chromium process trees on success, failure, timeout, or interruption. It fingerprints the default NervOS database before and after each run. Playwright traces and screenshots are retained only on failure under ignored output paths; temporary databases and logs are removed after process handles close.

The one Chromium journey uses the real UI, API, Worker, migrations, and opaque cookie session without MSW or external services. It proves fresh setup, dashboard identity, logout, login, browser-reload restoration, final logout, and direct `/dashboard` redirection to login. Since B4 it also proves deterministic two-provider portability: the same Agent Instance executes through the Anthropic double, reloads, is reconfigured to OpenAI, executes again, and shows both immutable provider/model snapshots after another reload. Since C7 it proves execution observability end to end: the retry Run's timeline is expanded in the browser and asserted row by row in order and exactly once — accepted, queued, claimed, started, the first failure, the scheduled retry with its due instant, the second claim, the second start, and the success — then the page is reloaded and the identical timeline is rebuilt from durable server state; the cancelled Run's timeline is asserted to be exactly its two cancellation facts, with nothing fabricated to fill the gap that the absence of an `attempt.cancelled` event leaves. The supervisor removes both `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` from every child environment and installs two distinct offline provider doubles, so the journey cannot reach a real provider even when the operator has credentials exported. Unexpected non-loopback browser requests are rejected. Aggregate `check` remains E2E-free; run both `check` and `e2e` for full local verification, which is exactly what `.github/workflows/ci.yml` does in two separate jobs.

Troubleshooting:

- Missing browser: run `pnpm --dir apps/web exec playwright install chromium`.
- API or Vite early exit: inspect the bounded supervisor diagnostic printed for that phase.
- Origin mismatch: use the supervisor rather than manually mixing `localhost` and `127.0.0.1`.
- Port handoff failure: rerun; Vite strict-port mode fails instead of silently changing the Origin.
- Failing only on a Linux CI runner: the `e2e` job runs `pnpm --dir apps/web exec playwright install-deps chromium` for runner system libraries; local bootstrap never installs OS packages.

## Continuous integration and security scanning

Two workflows under `.github/workflows/` run on pull requests, pushes to `main`, and a weekly schedule (security only):

- `ci.yml` — a browser-free `check` job (`bootstrap.py --skip-browser` then `scripts/check.py check`) and a separate `e2e` job (full bootstrap, CI-only Playwright system libraries, then `scripts/check.py e2e`).
- `security.yml` — `scripts/security_scan.py --tracked-only`, using only the standard library.

Both declare `permissions: contents: read`, reference no secrets, use `pull_request` rather than `pull_request_target`, and pin every action to a verified full-length commit SHA. No external AI provider or third-party service participates. Full details, including the pinning policy, cache strategy, scanner rules, and troubleshooting, are in [continuous integration](ci.md).

## Clean-environment verification

`uv run python scripts/clean_check.py` (or `make clean-check`) proves the current working tree is self-contained. It copies the exact file set Git considers part of the repository into a fresh temporary directory, initializes an isolated Git repository there with `git add -A`, and runs the lockfile check, bootstrap, `check`, the production build, and `e2e`. No commit is created, so no Git author identity is required, and the active checkout is only ever read. This verifies the uncommitted working tree as if it were a fresh clone.

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

The current migration head is `0006_stage_c6_queue_partitions`. `0006` adds one table, `queue_partitions(agent_instance_id, last_served_attempt_id)`, which stores durable per-Agent fairness history and nothing else — no lease, no status, and no active or pending counter. It deterministically backfills one row per Agent Instance that already has Jobs, taking `MAX(job_attempts.id)` as the marker, or `NULL` for an Instance that has never been claimed, so pre-existing data behaves as never served. `0001`–`0005` are unchanged. Downgrade drops only this derived scheduling metadata without rewriting execution history, and a later re-upgrade reconstructs it from the same backfill.

The Worker pins the schema revision it expects and refuses to start against a different one, so an upgrade across a migration is a normal migrate-then-restart transition; no mixed-revision rolling operation is supported.

Use a unique file-backed database for migration/database tests, set it with `NERVOS_DATABASE_PATH`, and dispose SQLAlchemy engines before Windows cleanup. Every connection enables foreign keys and a 5000 ms busy timeout; A2 does not enable WAL. Timestamps pass through `UTCDateTime`, which rejects naive writes and restores UTC-aware values. See `docs/database.md` for the schema and migration commands.

## A3 authentication development

First-run setup must be performed while the empty installation is reachable only through loopback or another trusted interface. Every unsafe `/api/v1` request requires an `Origin` header exactly equal to `NERVOS_APP_ORIGIN`; this includes manual API calls to setup, login, and logout. Setup and login are JSON-only and bounded before parsing. Process-local Argon2 concurrency returns a recoverable 429 under saturation but does not replace reverse-proxy/network rate limiting beyond loopback. A3 adds no CORS; A4 uses the same-origin Vite proxy rather than weakening that boundary.

Authentication/database tests always use Alembic-migrated, file-backed temporary databases. The setup concurrency test uses independent SQLite connections and proves that `BEGIN IMMEDIATE` permits exactly one initial administrator. See `docs/authentication.md` for the complete contract.

## Documentation workflow

After a milestone:

- update `docs/implementation-status.md` only after verification passes
- update behavior docs when behavior changes
- add an ADR for significant architecture decisions
- never describe future target features as already implemented

## Testing philosophy

Use many fast unit tests, integration tests at real boundaries, and a small number of E2E tests for critical journeys. Tests must be deterministic, order-independent, and isolated from developer data.

Stage A's A5 E2E journey is:

```text
fresh migrated install -> setup -> dashboard -> logout -> login -> dashboard -> reload -> dashboard -> logout -> direct protected route -> login
```

### Durable execution and observability

Execution tests drive the **real** persistence, reclamation, retry, cancellation, and Worker modules; a hand-written fixture that fabricates a transition could agree with itself while disagreeing with production. Two conventions make that practical and keep CI bounded.

**Time is a parameter, never a wait.** A lease expires and a retry becomes due by passing a later `now=` into persistence calls, and a Worker's auxiliary poll intervals are parked far out so a test drives those transitions explicitly instead of racing a timer. Only the journeys where the process loop itself is under test use the real clock.

**Every database is disposable.** Tests migrate into `tmp_path` and set `NERVOS_DATABASE_PATH` to point there. The developer's `~/.nervos/nervos.db` is never migrated, read, or written: the migration suite carries an autouse fixture that fingerprints it before and after every test and fails on any change, and no suite calls a live provider.

When adding to one of these areas:

- **Event pagination** is a keyset cursor on `sequence`. Test it by draining pages with `after_sequence` and asserting the collected sequences are contiguous with no duplicates, not by asserting a page size.
- **Query plans** are asserted by capturing the statements the code really issues and replaying them through `EXPLAIN QUERY PLAN`, checking the *shape* (an index range seek scoped by `run_id`, no `SCAN`, no temporary B-tree). Asserting the plan's exact string would fail on an unrelated SQLite version change.
- **Concurrency** is proved with separate engines over one database file and a `threading.Barrier`, never with a forked process. Nothing in the suite relies on `fork`, `fcntl`, Unix signals, Unix sockets, or filesystem locks, which is what keeps it honest on Windows.
- **Restart** is simulated by disposing and reopening the database engine and rebuilding the runtime composition inside the test process. That establishes that no correctness depends on in-memory state; it does not spawn a second interpreter, and it does not claim to.

### Integrated Stage C acceptance

The Stage C acceptance suites live under `tests/integration/` and are part of the canonical `pytest` run:

| Suite | What it establishes |
| --- | --- |
| `test_stage_c_acceptance.py` | The A–L composition matrix: recovery, retry, cancellation, timeout, the three concurrency dimensions, fairness, backpressure, and timeline/durable-state agreement |
| `test_stage_c_restart.py` | Queued work, an exact retry instant, fairness markers, Worker incarnation, and every Run and Event surviving teardown and reopen |
| `test_stage_c_stress.py` | A bounded deterministic 120-Job soak over 4 Agent Instances, 2 providers and 3 heterogeneous Workers, plus SQLite integrity checks |

C8 is acceptance, not architecture: it adds no API, status, table, migration, policy, transport, configuration, or authority mechanism. If integrated acceptance ever appears to need one, that is a stop-and-review signal rather than something to absorb into a test file.

## Review workflow

Review each milestone before beginning the next. Use the architecture reviewer for boundary changes, the security reviewer for authentication/secrets/permissions/tool/file execution, and the test reviewer before declaring milestone completion.
