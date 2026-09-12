# NervOS database foundation

Milestone A2 uses synchronous SQLAlchemy 2 with SQLite through `sqlite+pysqlite`. Alembic is the sole schema authority; application and test code must not call `Base.metadata.create_all()`.

## Connection behavior

Database URLs are constructed with SQLAlchemy `URL.create`, including on Windows paths containing spaces or URL-significant characters. Every application and Alembic DBAPI connection enables:

- `PRAGMA foreign_keys=ON`
- `PRAGMA busy_timeout=5000`

A2 deliberately does not enable WAL. The busy timeout is a local lock-handling setting, not a worker or concurrency architecture.

## UTC timestamps

`UTCDateTime` accepts only datetimes with an effective timezone offset. It normalizes values to UTC, stores naive UTC because SQLite does not preserve offsets, and restores exact UTC awareness on reads. Nullable timestamp values pass through as `None`.

## Application schema

Revision `0001_stage_a` creates exactly two application tables. `alembic_version` and SQLite's `sqlite_sequence` are implementation tables rather than NervOS application tables.

### `users`

- `id`: generated integer primary key with SQLite `AUTOINCREMENT`
- `username`: unique, lowercase/trimmed canonical value of length 3–32
- `password_hash`: non-null Argon2id password hash; authentication hashing is implemented and plaintext passwords are never persisted
- `role`: non-empty text value
- `is_active`: non-null boolean, default true
- `created_at`, `updated_at`: non-null UTC timestamps with valid ordering

Named constraints: `pk_users`, `uq_users_username`, `ck_users_username_canonical`, `ck_users_username_length`, `ck_users_role_nonempty`, and `ck_users_timestamp_order`.

The schema is a general users schema. It does not force ID 1, enforce a singleton administrator, seed a user, or implement user management.

### `auth_sessions`

- `id`: generated integer primary key with SQLite `AUTOINCREMENT`
- `user_id`: non-null foreign key to `users.id`
- `token_hash`: unique non-null `BLOB(32)` intended only for a digest
- `created_at`, `expires_at`: non-null UTC timestamps with expiration after creation
- `revoked_at`: nullable UTC timestamp, never earlier than creation

Named constraints: `pk_auth_sessions`, `fk_auth_sessions_user_id_users`, `uq_auth_sessions_token_hash`, `ck_auth_sessions_expiration_order`, and `ck_auth_sessions_revocation_order`.

Named indexes: `ix_auth_sessions_user_id` and `ix_auth_sessions_expires_at`.

SQLite does not enforce the declared BLOB length. A3 produces an exact digest and never stores a raw session token.

## Migrations

Set `NERVOS_DATABASE_PATH` to the intended database, then run:

```bash
uv run alembic -c apps/api/alembic.ini upgrade head
uv run alembic -c apps/api/alembic.ini current
uv run alembic -c apps/api/alembic.ini check
```

The development API launcher runs `upgrade head` before Uvicorn. Direct production Uvicorn startup requires an explicit successful migration first.

Downgrade to `base` is supported for disposable verification databases:

```bash
uv run alembic -c apps/api/alembic.ini downgrade base
```

This downgrade deletes both application tables and all their data. Never run destructive migration tests against the default or another non-disposable database.

A3 uses this existing schema for setup and authentication without a migration. Passwords are stored only as Argon2id hashes. Session tokens are generated from 32 random bytes and persisted only as 32-byte binary SHA-256 digests. Initial setup reserves SQLite's writer with `BEGIN IMMEDIATE` before checking for any user, then commits the first admin and initial session atomically. No raw token, plaintext password, setup marker, conversation session, agent, job, worker, memory, or other runtime table is added.
