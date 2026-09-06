---
name: test-change
description: Add or improve tests for a NervOS change using the project's testing pyramid, including regression, API/database integration, frontend behavior, and E2E coverage.
---

# Test a NervOS Change

Read `.claude/rules/testing.md`.

## Process

1. Identify externally observable behavior changed.
2. Inspect neighboring tests.
3. Choose the lowest-cost test level that proves the behavior.
4. Add integration/E2E only when a real boundary/user journey justifies it.
5. Include failure/boundary cases proportional to risk.
6. Run the narrowest test first.
7. Run the affected package/suite.
8. Confirm tests are deterministic and isolated.

## Common cases

API:
- success
- validation failure
- unauthenticated access
- unauthorized ownership where relevant
- not-found/conflict behavior

Authentication:
- valid credentials
- invalid credentials
- expired session
- revoked session
- setup locked after initialization

Frontend:
- loading
- success
- error
- redirect/navigation behavior

Never call real external AI providers in Stage A tests.
