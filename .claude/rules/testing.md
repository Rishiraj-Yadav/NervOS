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