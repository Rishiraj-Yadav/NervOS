# NervOS Implementation Status

## Current phase

Stage A — Foundation

## Stage A milestones

- [x] A0 — Stage A plan reviewed
- [x] A1 — Repository and tooling bootstrap
- [x] A2 — FastAPI + typed configuration + SQLite + SQLAlchemy + Alembic
- [x] A3 — First-run setup + local authentication
- [ ] A4 — React dashboard foundation
- [ ] A5 — Stage A E2E flow
- [ ] A6 — CI and quality gates
- [ ] A7 — Final architecture/security/test audit

## Stage A acceptance criteria

- [ ] clean checkout can bootstrap dependencies
- [x] API starts successfully
- [x] `/api/v1/health` succeeds
- [x] migrations work from empty database
- [x] first user can initialize NervOS exactly once
- [x] user can log in
- [x] authenticated session survives browser refresh
- [x] logout invalidates server-side session
- [ ] protected dashboard is unavailable unauthenticated
- [x] backend tests pass
- [ ] frontend tests pass
- [ ] E2E smoke flow passes
- [x] Ruff passes
- [x] Pyright passes
- [x] frontend TypeScript check passes
- [ ] production frontend build passes
- [ ] CI matches local quality commands
- [ ] no real secrets are committed

## Not implemented in Stage A

- AgentPackage/AgentInstance execution
- job queue/workers
- scheduler/event router
- model providers
- MCP/tool gateway
- agent memory
- agent conversation sessions
- package installer
- marketplace
- IoT
- multi-agent orchestration

## A1 verified checks

- `uv sync --frozen --all-packages`
- `uv run python scripts/check.py check`
- Ruff check and format check
- Pyright strict type checking
- Pytest tooling suite (6 tests)
- ESLint and TypeScript checks
- Vitest foundation (`--passWithNoTests`; product tests begin in A4)
- Workspace membership, ignored-file, syntax, lockfile, and no-product-behavior audits

## A2 verified checks

- `uv sync --frozen --all-packages`
- `uv lock --check`
- `uv run python scripts/check.py lint`
- `uv run python scripts/check.py typecheck`
- `uv run python scripts/check.py test`
- `uv run python scripts/check.py check`
- Pytest backend/tooling suite (51 tests)
- Isolated Alembic upgrade, current/head, no-drift check, downgrade, and re-upgrade
- Migration-first development launcher and exact `GET /api/v1/health` smoke check
- Ruff, Ruff format, Pyright strict, ESLint, TypeScript, and Vitest foundation checks

## A3 verified checks

- `uv sync --frozen --all-packages`
- `uv lock --check`
- `uv run python scripts/check.py lint`
- `uv run python scripts/check.py typecheck`
- `uv run python scripts/check.py test`
- `uv run python scripts/check.py check`
- Pytest backend/tooling suite (85 tests)
- Real file-backed setup race exercised 20 times with exactly one user/session
- Manual setup/login/me/logout/replay/login smoke flow over real Uvicorn
- Exact-origin and oversized-body pre-body middleware checks
- Alembic still head `0001_stage_a` with no schema drift
- Dedicated security/test/architecture reviewers: no CRITICAL/HIGH findings; concurrency bound, safe 503 mapping, pre-body Origin/size boundary, setup-complete precheck, and single-owner policy fixes applied

## Current work

A3 verification completed. First-run setup, Argon2id passwords, opaque server-side sessions, secure cookies, exact-Origin CSRF protection, and the setup/login/logout/me endpoints are ready for review. Authentication sessions remain distinct from future agent conversation sessions.

## Blockers

- GNU Make is not installed on the current Windows machine; use the documented Python command facade.
- Frontend tests have no product test files yet; Vitest exits successfully with `--passWithNoTests` by design until A4.
- FastAPI/Starlette's current TestClient dependency path emits two upstream deprecation warnings; tests still pass.
- Without an operator bootstrap secret, first-run setup is safe only while the API is reachable through a trusted interface; non-loopback deployments must enforce their own network/proxy rate limits.

## Next action

Review A3. Do not begin A4 until it receives explicit approval.

## Maintenance rule

Update this file after every accepted milestone. Do not check an item merely because code was generated; verify its acceptance criteria first.
