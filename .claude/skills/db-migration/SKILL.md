---
name: db-migration
description: Create or review a NervOS SQLAlchemy/Alembic schema migration safely, including models, upgrade/downgrade, constraints/indexes, SQLite compatibility, and migration tests.
---

# NervOS Database Migration

Read database and security rules first.

## Workflow

1. Identify the exact schema change and reason.
2. Inspect current models and latest Alembic revision.
3. Update SQLAlchemy models.
4. Create one focused migration.
5. Inspect generated operations manually.
6. Verify constraints/indexes are intentional.
7. Verify no plaintext secret/session-token storage is introduced.
8. Test upgrade from an empty DB.
9. Test upgrade from previous revision when fixtures exist.
10. Test downgrade unless documented as irreversible.
11. Run affected tests.

## Rules

- no unrelated schema refactors
- no manual schema changes as source of truth
- preserve SQLite compatibility
- use UTC timestamps
- use explicit foreign keys
- add indexes only when query patterns justify them
- document destructive/irreversible behavior
