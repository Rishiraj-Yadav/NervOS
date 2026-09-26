# ADR 0025 — Package Installation, Execution Resolution, and Lifecycle Semantics

Status: Accepted for G0 architecture freeze

This ADR freezes how installed package software is stored and become executable without creating a
second execution engine: node-global package software with owner-scoped AgentInstances; install
distinct from AgentInstance creation; the built-in + installed-package definition-resolver
architecture with its G1/G3 split; Worker-only package execution through an isolated package-host
subprocess over a bounded protocol; immutable exact executable identity on Runs; the durable
installation state machine with filesystem/database crash recovery; activation health-check,
upgrade, rollback, and uninstall semantics; and user-data retention. Canonical authority is
`docs/stage-g/README.md`.

## Context

Today the Worker resolves the trusted handler from a Run's frozen exact
`(agent_key, agent_definition_version)` identity and executes it in-process
(`application/run_execution.py`, `application/trusted_chat.py`), and the API/Scheduler each build
their own `create_builtin_definition_registry()`. Installing a package record is not executable
software. Stage G must let installed package software run through the existing Run → Job → Attempt
→ Worker path — reusing Stage-C reliability, Stage-D grants, and Stage-F context — while keeping
package code out of the Worker interpreter and never forking the execution engine.

## Decision

### Installation ownership model

Installed package software/version is **node-global**; AgentInstances and all user/runtime state
are **owner-scoped**. Install does NOT automatically create an AgentInstance; creating/configuring
instances is a distinct owner operation. Future package lifecycle operations share one application
authority (`PackageApplicationService`) reached by CLI, API, and UI; the developer-side G2 builder
is separate from node installation authority.

### Definition resolution (G1/G3 split)

G1 delivers only the resolver contracts and composite semantics: an `InstalledPackageDefinitionSource`
interface beside the unchanged built-in source, deterministic in-memory/static test sources,
**exact-version resolution, collision failure, and built-in protection** — with **no durable package
tables**. G3 delivers the package-registry migration, the SQL-backed installed-package source, and
restart-safe persistent resolution. Resolution is `exact id or fail`; there is never "closest",
"latest", fallback, or implicit upgrade, and third parties cannot shadow or use the `nervos.*`
namespace.

### Worker-only package execution

Package code is **never imported into the long-lived Worker interpreter**. The future shape is:

```text
Run → Job → Attempt → Worker → PackageExecutionAdapter
  → exact package environment Python → package-host subprocess → nervos-sdk entrypoint
```

The Worker remains the single Stage-C authority for claiming Jobs, creating Attempts, and owning
retry/cancellation/timeout/scheduling disposition. The package host never claims Jobs, never
creates Attempts, never decides retries, never controls scheduling, never owns cancellation, never
accesses the database directly, and never receives raw credentials. The host executes package
policy inside **one already-authoritative Attempt** — it is not another Worker or execution engine.
This is one process boundary through which a bounded protocol carries initialization/handshake,
immutable Run configuration and context, package metadata, model request/response, tool
request/response, logging/telemetry, result, normalized error, and cancellation/termination.
Worker-side authority (providers, credentials, ToolLoop, call-time grants, tool audit,
cancellation, timeout, result mapping, retry disposition) does not cross the boundary.

Package-host failure uses the existing Stage-C failure-disposition philosophy: unknown/
non-graceful failure remains fail-closed/ambiguous unless the existing architecture proves it
safely retryable. Stage G adds no new retry class.

### Immutable executable identity

A Run accepted under package version v1 executes v1. G3 must pin the exact installed executable
identity (the frozen package/version/install reference) alongside the Run's existing immutable
snapshot so queued, retrying, recovery, and restart-recovered execution never rebind to a later
version merely because an upgrade happened.

### Installation state machine and crash recovery

Durable lifecycle states: `INSTALLING → INSTALLED → ACTIVE`; failure `FAILED`; removal
`PENDING_REMOVAL → REMOVED`. A filesystem-only orphan staging directory created before a DB row
exists is **not** a durable state; startup reconciliation treats it as debris. Crash recovery is
defined for: (1) staging created with no DB row — cleanup of age-bounded orphan staging; (2) DB
`INSTALLING` with activation rename absent — verify staged files then complete or mark `FAILED`,
previous version untouched; (3) files activated with DB `ACTIVE` missing — idempotent re-commit or
`PENDING_REMOVAL`; (4) environment preparation failure — durable `FAILED`, never `ACTIVE`;
(5) activation health-check failure — non-active, previous version untouched; (6) crash during
removal — complete removal when obligations are gone, otherwise stay disabled/`INSTALLED`. A SQLite
transaction plus a filesystem rename is not one ACID transaction; reconciliation converges the
lifecycle. Package versions are immutable once installed, paths are derived safely (raw package IDs
never trusted as filesystem paths), staging is same-volume with the authoritative store, and
activation may rely on same-volume atomic rename.

### Activation health check

Before a package becomes `ACTIVE`, and only after explicit operator trust: environment complete,
entrypoint exists, SDK/protocol compatibility verified, package-host subprocess starts, and a
bounded handshake completes. This is the first permitted execution of trusted package code; manifest
parsing and inspection never execute code. Failure leaves previous versions untouched.

### Upgrade, rollback, uninstall

- **Upgrade:** installing v2 does not automatically rebind existing AgentInstances. An explicit
  operator upgrade/rebind validates v2, validates configuration (requiring a valid new
  configuration from the operator if the schema changed — no migration scripts), shows the
  capability/tool delta, then future Runs use v2. Queued/running/retryable Runs accepted under v1
  remain v1.
- **Rollback:** means future binding v2 → v1 only. It never rewinds Runs, reverses tool side
  effects, rewrites conversations, restores memory, or undoes external actions; V1 has no
  package-owned DB migrations or config-migration hooks, so no impossible rollback is promised.
- **Uninstall:** is not deleting a directory. It accounts for AgentInstances, queued/running/
  retryable Runs, Stage-E triggers, conversations, memory, audit, and snapshots/history; package
  code is physically removed only when no executable obligations require it, with
  `PENDING_REMOVAL` where appropriate. User/runtime historical data survives package removal.

## Consequences

Stage G gains a single, restart-safe, convergent install/execution lifecycle without a second
execution engine and without weakening Stage-C/D/E/F authority. The subprocess boundary preserves
dependency isolation (content-addressed package environments) but is an architectural contract, not
sandboxing — hardening belongs to Stage H. Instances, Runs, and history stay owner-scoped and
survive package removal, which keeps uninstall honest.