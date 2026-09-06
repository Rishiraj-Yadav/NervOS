---
paths:
  - "apps/api/**/*.py"
  - "packages/**/*.py"
---

# Python Backend Rules

- Use type annotations on public functions.
- Use Pydantic models at API boundaries.
- Route handlers should be thin.
- Put application logic into services/core modules.
- Do not expose SQLAlchemy ORM objects directly as API responses.
- Do not use wildcard imports.
- Use dependency injection for database sessions.
- Prefer explicit exceptions over generic `except Exception`.
- Preserve exception causes when wrapping errors.
- Use timezone-aware UTC datetimes.
- Generate identifiers server-side.
- Add tests with behavioral changes.
- Use Ruff formatting/linting.
- Keep async functions non-blocking.