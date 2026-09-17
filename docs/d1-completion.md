# D1 Completion Report — Durable Tool, Capability, and Audit Schema

**Milestone:** D1 — Durable tool, capability, and audit schema  
**Status:** Complete and accepted  
**Date:** 2026-09-17

## Purpose

D1 makes the Stage D tool, capability and tool-audit model durable. It creates four tables and extends three, and **nothing in it executes, discovers, authorizes or dispatches a tool**. The permission decision, the MCP client, the registry and the Think-Act-Observe loop all belong to later milestones; this migration only ensures that when they arrive, the states they need are the states the database already permits, and the states they must never reach are states it refuses to hold.

## What was delivered

### Migration 0007

One Alembic migration: `0007_stage_d1_tool_capability_audit.py`

**Four new tables:**

1. **`mcp_connections`** — durable *definition* of an approved tool source (configuration, never a live client)
   - Owner-scoped (`owner_user_id` FK)
   - Transport (`stdio` | `http`)
   - Endpoint or operator-declared server key
   - Optional credential reference (operator-declared alias, CHECK-constrained to lowercase kebab)
   - Catalog status, last discovery time, safe last-error
   - **No column can hold a credential value**
   - **No column exists for a stdio command** (operator-declared only, per ADR 0016)

2. **`tool_definitions`** — one callable operation per (source_kind, source_id, upstream_name)
   - Identity enforced by **two partial unique indexes plus a pairing CHECK**, not one three-column UNIQUE (SQLite does not enforce uniqueness over NULL, so a plain UNIQUE would accept unlimited duplicate built-ins)
   - Built-in: `source_id IS NULL`, `source_kind = 'builtin'`
   - MCP: `source_id` FK to `mcp_connections`, `source_kind = 'mcp'`
   - Model-facing name (persisted, never recomputed)
   - Canonical input/output schema (JSON text)
   - Fingerprint (sha256 over the model-facing definition)
   - Four `hint_*` columns for untrusted source annotations (presentation metadata only)

3. **`agent_tool_grants`** — the authority that exists *now*
   - **The row's existence is the authority** (no DENY row; "never granted" and "revoked" are the same state)
   - `UNIQUE(agent_instance_id, tool_definition_id)`
   - `id` is `INTEGER PRIMARY KEY AUTOINCREMENT` (monotonic cutoff primitive for `runs.tool_grant_cutoff_id`)
   - `reviewed_fingerprint` — the definition the user approved
   - `created_at` is display metadata only (no timestamp decides authority)

4. **`tool_invocations`** — durable audit of calls that actually happened
   - Cannot hold a secret: no column for arguments, results, credentials, tokens, or raw provider payloads
   - Arguments: sha256 digest + byte count + bounded key/type skeleton (no values)
   - Results: sha256 digest + byte count + truncation flag
   - Identity snapshot (`source_kind`, `source_id`, `upstream_name`, `model_name`) so audit survives deletion
   - Seven-state lifecycle: `requested`, `denied`, `cancelled`, `started`, `succeeded`, `failed`, `ambiguous`
   - `started_at` is the ambiguity boundary (committed immediately before dispatch)
   - `tool_sequence` is the ordering authority (1..N per Attempt)

**Three existing tables extended:**

1. **`runs`** — five new columns
   - Four immutable loop limits: `max_tool_calls`, `tool_timeout_ms`, `tool_result_max_bytes`, `max_consecutive_tool_failures`
   - `tool_grant_cutoff_id` — durable monotonic cutoff (not a clock)
   - Every default is legacy-compatible (migrated Runs carry `max_tool_calls = 0` and cutoff 0)

2. **`job_attempts`** — three nullable usage columns
   - `input_tokens`, `output_tokens`, `total_tokens` for multi-turn crash-durable accounting
   - NULL for every Stage C Attempt (one-call Attempts report usage on the Run at terminalization)

3. **`run_events`** — nullable `tool_invocation_id` FK plus six tool event types
   - `tool.requested`, `tool.started`, `tool.succeeded`, `tool.failed`, `tool.denied`, `tool.ambiguous`
   - The thirteen Stage C event types remain a byte-for-byte prefix
   - `tool.cancelled` deliberately absent (ADR 0017 keeps `run.cancelled` as cancellation truth)

### ORM models

Four new SQLAlchemy model classes in `packages/nervos-core/src/nervos_core/infrastructure/database/models.py`:

- `McpConnection`
- `ToolDefinition`
- `AgentToolGrant`
- `ToolInvocation`

Plus the five new columns on `Run` and three on `JobAttempt`, and the extended `run_events.event_type` CHECK.

### Tests

**Schema constraint tests** (`packages/nervos-core/tests/integration/test_tool_schema_tables.py`):
- 42 tests proving what the database refuses and what it permits
- Load-bearing: built-in identity (NULL `source_id` does not defeat uniqueness)
- No column in any Stage D table can hold a secret (structural assertion)
- Lifecycle state machines hold (non-dispatched states cannot claim `started_at`)
- Foreign-key `ON DELETE RESTRICT` prevents history cascade
- Credential reference and server key are CHECK-constrained to lowercase kebab (environment-variable names unrepresentable)

**Migration lifecycle tests** (`apps/api/tests/integration/test_migrations_d1.py`):
- 21 tests proving upgrade, downgrade refusal, and frozen-history protection
- Downgrade refuses if any Stage D-only state is present (preserves durable evidence)
- Frozen migrations: byte-for-byte sha256 verification that 0001–0006 are unchanged
- Default database untouched (every test uses a disposable temp DB)

**Architecture boundary tests** (extended in `packages/nervos-core/tests/architecture/test_boundaries.py`):
- `EXPECTED_TABLES` extended with the four Stage D tables
- Migration filename list extended to include `0007_stage_d1_tool_capability_audit`
- No weakening of existing guards

### Query plans

All six critical queries verified as SEARCH operations on their intended indexes:

```
owner-scoped connections: SEARCH mcp_connections USING INDEX ix_mcp_connections_owner_user_id_id (owner_user_id=?)
definitions by source: SEARCH tool_definitions USING COVERING INDEX ix_tool_definitions_source_id (source_id=?)
definition by identity: SEARCH tool_definitions USING COVERING INDEX uq_tool_definitions_mcp (source_id=? AND upstream_name=?)
exact grant lookup: SEARCH agent_tool_grants USING COVERING INDEX sqlite_autoindex_agent_tool_grants_1 (agent_instance_id=? AND tool_definition_id=?)
grants for one agent: SEARCH agent_tool_grants USING COVERING INDEX sqlite_autoindex_agent_tool_grants_1 (agent_instance_id=?)
replay predicate: SEARCH tool_invocations USING INDEX sqlite_autoindex_tool_invocations_1 (attempt_id=?)
```

No scan of a growing table. The replay predicate (§46 of the Stage D plan) uses the `UNIQUE(attempt_id, tool_sequence)` index prefix as designed.

## What was verified

### Full test suite

```
Core package: 116 tests passed
D1 schema constraints: 42 tests passed
D1 migration lifecycle: 21 tests passed
```

All D1 tests green. No regressions in A1, B1–B4, or C1–C8 suites.

### Acceptance criteria (from Stage D plan P64)

✅ **`0007` upgrades and downgrades cleanly on a temp DB**  
✅ **`EXPECTED_TABLES` and the migration list are extended**  
✅ **The Worker refuses a stale revision** (`EXPECTED_SCHEMA_REVISION` bumped to `0007_stage_d1_tool_capability_audit`)  
✅ **Four tables exist with their constraints**  
✅ **Nothing executes** — D1 is schema-only; no registry, evaluator, or MCP client exists

### Schema-parity protection

The permanent integration test in `test_migrations_d1.py` asserts the migrated schema against the ORM metadata on:
- Constraint names
- Normalized CHECK expressions
- Server defaults
- Foreign keys
- Indexes

A negative control confirms the guard fails when a constraint is removed. This is the same schema-parity discipline Stage C established.

### Security properties verified

1. **No column in any Stage D table can hold a secret** — structural assertion over column names
2. **`credential_ref` and `server_key` are CHECK-constrained** to lowercase kebab, so environment-variable names are unrepresentable
3. **No column exists for a stdio command** — the migration refuses to add one
4. **Built-in identity is unique** — NULL `source_id` does not defeat the partial-index construction
5. **Foreign-key `ON DELETE RESTRICT`** — history cannot be cascaded away
6. **Downgrade refuses rather than destroys** — preserves durable evidence

## Protected files

Migrations `0001`–`0006` are byte-identical (verified by sha256). No other protected file changed.

## What did not change

- No route, no service, no Worker composition root
- No domain logic beyond the ORM model declarations
- No frontend
- No runtime behaviour
- The Worker still refuses to start against a schema it does not expect (`EXPECTED_SCHEMA_REVISION` check)

## Next milestone

**D2 — Capability grants and the call-time permission decision**

D2 implements:
- Grant CRUD service (transactionally re-checked ownership)
- Frozen precedence evaluator (hard denial > absence > explicit grant)
- Fail-closed defaults (no grant = denied)
- Security tests proving:
  - No grant ⇒ denied by default
  - Foreign grants are unreachable (indistinguishable from missing)
  - Revoked grant ⇒ denied at the next call
  - Drifted definition ⇒ denied until re-confirmed
  - MCP annotations cannot grant authority
  - Disabled connection denies
  - Invocation history is owner-scoped and survives deletion

D2 is pure authorization: no tools exist to call, no catalog to assemble, no loop to run. What is asserted is the set of grants the database permits and the set it refuses.

## Artifacts

- Migration: `apps/api/alembic/versions/0007_stage_d1_tool_capability_audit.py`
- ORM models: `packages/nervos-core/src/nervos_core/infrastructure/database/models.py` (extended)
- Schema tests: `packages/nervos-core/tests/integration/test_tool_schema_tables.py`
- Migration tests: `apps/api/tests/integration/test_migrations_d1.py`
- This report: `docs/d1-completion.md`
- Updated: `docs/implementation-status.md`

---

**D1 is complete and accepted.**
