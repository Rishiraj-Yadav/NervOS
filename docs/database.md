# NervOS database foundation

Milestone A2 uses synchronous SQLAlchemy 2 with SQLite through `sqlite+pysqlite`. Alembic is the sole schema authority; application and test code must not call `Base.metadata.create_all()`.

**Note:** This document describes the foundation schema through early milestones (A2/B1). The current schema includes additional tables from Stages C, D, E, F, G, and H. See `docs/implementation-status.md` and the Alembic migrations (`apps/api/alembic/versions/`) for the complete delivered schema.

## Connection behavior

Database URLs are constructed with SQLAlchemy `URL.create`, including on Windows paths containing spaces or URL-significant characters. Every application and Alembic DBAPI connection enables:

- `PRAGMA foreign_keys=ON`
- `PRAGMA busy_timeout=5000`

A2 deliberately does not enable WAL. The busy timeout is a local lock-handling setting, not a worker or concurrency architecture.

## UTC timestamps

`UTCDateTime` accepts only datetimes with an effective timezone offset. It normalizes values to UTC, stores naive UTC because SQLite does not preserve offsets, and restores exact UTC awareness on reads. Nullable timestamp values pass through as `None`.

## Application schema

Revision `0001_stage_a` creates the two Stage A tables. Revision `0002_stage_b1_agent_instances_runs` additively creates `agent_instances` and `runs`, for exactly four application tables. `alembic_version` and SQLite's `sqlite_sequence` are implementation tables rather than NervOS application tables.

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

### `agent_instances`

B1 stores explicit user-owned configuration pinned to an exact trusted definition version: owner, key/version, non-unique display name, enabled state, canonical model-provider identifier, opaque bounded model name, and UTC timestamps. The owner FK is restrictive. Duplicate names and multiple instances of one definition are allowed. The `(owner_user_id, id)` index supports cursor listing.

### `runs`

B1 stores one immutable request snapshot per instance: exact definition/provider/model/input and positive effective execution limits. Lifecycle is exactly `created -> running -> succeeded|failed`; a database CHECK enforces complete state-dependent field shapes, and snapshot text must contain a character outside the frozen NervOS blank-text set, so every stored row reconstructs as a valid domain Run. Row text is bounded by that row's snapshotted limits, usage is independently nullable/nonnegative, and terminal elapsed time is nonnegative. The instance FK is restrictive and `(agent_instance_id, id)` supports newest-first cursor history. Temporary Stage B maxima and the one-call policy are enforced in domain/application definition policy rather than fossilized as schema maxima.

Neither migration nor setup seeds an Agent Instance or Run. No Agent Definition, provider, Job, Attempt, conversation, message, secret, cost, tool, or memory table exists.

## Stage H security tables

Stage H adds six tables through six migrations, with `0019_stage_h4_sandbox_policy` a deliberate
no-op marker because the H4 policy is code rather than schema.

| Revision | Tables |
|---|---|
| `0016_stage_h1_secret_manager` | `secrets`, `secret_keys` |
| `0017_stage_h2_account_connections` | `account_connections` |
| `0018_stage_h3_action_approvals` | `action_approvals` |
| `0019_stage_h4_sandbox_policy` | none (no-op marker) |
| `0020_stage_h5_publisher_trust` | `publisher_trust` |
| `0021_stage_h_approval_events` | no table; extends the safe Run-event vocabulary |
| `0022_stage_h_account_oauth` | `account_oauth_requests`; refresh and revocation fields on `account_connections` |
| `0023_worker_sandbox_capability` | no table; bounded sandbox-capability columns on `workers` |

The delivered local migration head is **`0023_worker_sandbox_capability`**, and both the Worker and
the Scheduler refuse to start on any other revision.

- **`secrets`** holds encrypted `ciphertext` plus the nonce and authentication tag, never a
  plaintext value. A check constraint enforces that revoking a secret destroys its stored value.
- **`secret_keys`** holds the key version so rotation is auditable. The key material itself is
  **not** in the database — it lives in the file named by `NERVOS_SECRETS_KEY_FILE`.
- **`account_connections`** binds an owner to a provider, its scopes, expiry, refresh lease and
  provider-revocation outcome. It never stores a token.
- **`account_oauth_requests`** stores a one-time, owner-bound hash of OAuth `state`, an expiry and
  a reference to the encrypted PKCE verifier. It never stores the raw state or verifier.
- **`action_approvals`** is the durable approval record: the exact action, its redacted preview and
  a digest binding the request, the Run/Attempt, the resolved owner, the expiry, and who approved
  it. `tool.approval_requested` and `tool.approval_decided` record safe, linked timeline evidence.
- **`publisher_trust`** records the local trust decision for a signer fingerprint and its
  revocation, so a revoked publisher is blocked at install, rebind, and new execution.
- **`workers`** additionally carries the bounded sandbox capability the Worker observed for
  itself — `platform`, `sandbox_backend`, `package_execution_supported`. It is observed health,
  never execution authority: the launch factory re-decides at every package start, expired
  Workers do not count as available, and no Worker identity, hostname or path is published.

The `0016`, `0017`, `0018`, `0020`, `0021`, `0022`, and `0023` migrations refuse to downgrade: Stage H's security state
must never be silently walked backwards.

## Migrations

Set `NERVOS_DATABASE_PATH` to the intended database, then run:

```bash
uv run alembic -c apps/api/alembic.ini upgrade head
uv run alembic -c apps/api/alembic.ini current
uv run alembic -c apps/api/alembic.ini check
```

The development API launcher runs `upgrade head` before Uvicorn. Direct production Uvicorn startup requires an explicit successful migration first.

**`downgrade base` is no longer available.** Stage H's H1, H2, H3 and H5 migrations refuse to
downgrade unconditionally: dropping the secret, connection, approval or publisher-trust tables
would destroy security evidence irrecoverably. The refusal happens before any DDL, so a refused
downgrade leaves the schema exactly as it was rather than half-dropped.

A database that predates Stage H can still be walked back to `0015_runtime_integration` and below,
which is why the pre-Stage-H migration lifecycle tests start there. Anything at or above
`0016_stage_h1_secret_manager` must be restored from a backup instead. Never run destructive
migration tests against the default or another non-disposable database.

A3 uses this existing schema for setup and authentication without a migration. Passwords are stored only as Argon2id hashes. Session tokens are generated from 32 random bytes and persisted only as 32-byte binary SHA-256 digests. Initial setup reserves SQLite's writer with `BEGIN IMMEDIATE` before checking for any user, then commits the first admin and initial session atomically. No raw token, plaintext password, setup marker, conversation session, agent, job, worker, memory, or other runtime table is added.
