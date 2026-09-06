---
paths:
  - "packages/nervos-core/src/nervos_core/database/**/*.py"
  - "apps/api/alembic/**/*.py"
---

# Database Rules

- SQLAlchemy 2.x patterns only.
- Every schema modification requires an Alembic migration.
- Migrations must support upgrade and downgrade unless technically impossible.
- Foreign keys must be explicit.
- Add indexes for frequently queried identifiers and foreign keys where useful.
- Store timestamps as UTC.
- Never store plaintext passwords or session tokens.
- Do not mix schema migrations with unrelated feature changes.
- SQLite is the primary Stage A database.