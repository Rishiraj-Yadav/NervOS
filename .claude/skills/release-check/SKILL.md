---
name: release-check
description: Run the NervOS release-readiness checklist: tests, lint, type checks, migrations, frontend build, E2E, security review, documentation/status consistency, and artifact sanity. Manual invocation recommended.
disable-model-invocation: true
---

# NervOS Release Check

Do not add features during a release check.

## Verify

Backend:
- lock/dependencies valid
- Ruff
- Pyright
- pytest
- migrations from empty DB

Frontend:
- lockfile install
- lint
- unit tests
- TypeScript check
- production build

E2E:
- critical user journeys

Security:
- no committed secret material
- authentication/session tests pass
- security reviewer for security-sensitive changes

Docs:
- README commands work
- implementation status matches reality
- roadmap does not claim future features exist
- ADRs cover architecture decisions

Git:
- inspect status/diff
- find accidental generated files/binaries

Return PASS/FAIL, blockers, warnings, and exact checks executed. Do not commit, push, or release unless explicitly requested.
