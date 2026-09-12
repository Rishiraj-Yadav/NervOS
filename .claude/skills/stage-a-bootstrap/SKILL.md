---
name: stage-a-bootstrap
description: Bootstrap, implement, or audit NervOS Stage A foundation: repository tooling, FastAPI, SQLite, authentication, React dashboard foundation, tests, CI, and documentation. Use only for Stage A work.
disable-model-invocation: true
---

# NervOS Stage A Bootstrap

Read `.claude/CLAUDE.md`, architecture/security/testing rules, `docs/architecture.md`, and `docs/implementation-status.md` before changing files.

Inspect the repository first.

## Stage A includes

- workspace/repository setup
- uv Python workspace
- FastAPI
- typed settings
- SQLite
- SQLAlchemy
- Alembic
- first-user setup
- server-side authentication
- React/TypeScript/Vite dashboard
- API client
- tests
- lint/type checking
- CI
- documentation

## Stage A excludes

- agent runtime
- model providers
- MCP/tools
- queue/workers
- scheduler
- agent memory
- marketplace
- IoT
- multi-agent orchestration
- multi-user management

## Workflow

1. Determine the exact Stage A milestone requested.
2. Inspect current implementation and status.
3. Produce a small milestone-scoped plan.
4. Implement only that milestone.
5. Add/update tests.
6. Run relevant checks.
7. Fix failures caused by the change.
8. Update `docs/implementation-status.md` only after acceptance criteria pass.
9. Report changed files, verified commands, and remaining work.

Never mark all of Stage A complete while milestone criteria remain unverified.
