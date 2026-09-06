# NervOS Implementation Status

## Current phase

Stage A — Foundation

## Stage A milestones

- [ ] A0 — Stage A plan reviewed
- [ ] A1 — Repository and tooling bootstrap
- [ ] A2 — FastAPI + typed configuration + SQLite + SQLAlchemy + Alembic
- [ ] A3 — First-run setup + local authentication
- [ ] A4 — React dashboard foundation
- [ ] A5 — Stage A E2E flow
- [ ] A6 — CI and quality gates
- [ ] A7 — Final architecture/security/test audit

## Stage A acceptance criteria

- [ ] clean checkout can bootstrap dependencies
- [ ] API starts successfully
- [ ] `/api/v1/health` succeeds
- [ ] migrations work from empty database
- [ ] first user can initialize NervOS exactly once
- [ ] user can log in
- [ ] authenticated session survives browser refresh
- [ ] logout invalidates server-side session
- [ ] protected dashboard is unavailable unauthenticated
- [ ] backend tests pass
- [ ] frontend tests pass
- [ ] E2E smoke flow passes
- [ ] Ruff passes
- [ ] Pyright passes
- [ ] frontend TypeScript check passes
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

## Current work

None yet.

## Blockers

None recorded.

## Next action

Run Stage A planning prompt A0 and review Claude's implementation plan before allowing file changes.

## Maintenance rule

Update this file after every accepted milestone. Do not check an item merely because code was generated; verify its acceptance criteria first.
