# ADR 0037 — Stage-H security repair contract

Status: Accepted by explicit user approval on 2026-10-03.

## Authority

The user approved the repair plan in
`docs/stage-h/verification-review-2026-10-03.md`, including required migrations and
refusal of package execution on platforms until actual isolation is qualified.
This additive decision supersedes contradictory H closeout claims and the
next-Run approval consequence in ADR 0034. It preserves the other stage boundaries.

## Decisions

- H1 resolves keys by ciphertext version. An explicit operator maintenance command
  re-encrypts bounded batches transactionally; reruns skip already migrated rows.
  An active key or key referenced by any recoverable ciphertext cannot be retired.
  Rotation does not silently destroy the old material. Old key files are provisioned
  explicitly alongside the configured current key and never exposed over HTTP.
- H2 implements operator-configured authorization-code PKCE S256 and encrypted
  token custody. Only NervOS-owned connector transports attach account credentials.
  Broker dispatch rechecks live owner/Run/Agent/tool permission, connection state,
  scope and token expiry. Refresh is bounded outside DB transactions with versioned
  updates to prevent concurrent stale token publication. Empty provider config refuses.
- H3 pending approval holds the same live Attempt and invocation, bounded by existing
  execution timeout and approval expiry. The Worker may continue other Runs within
  its configured concurrency. Owner decisions resume this exact invocation without
  replaying model calls or remote effects. Lease loss/restart/cancellation deny
  pending authority; an owner must submit a new Run after such termination. No
  cross-Attempt resumption is promised. Consumption and invocation start share one
  transaction after all permission/fencing predicates. Durable requested/decided
  events contain only static safe public copy.
- H4 resource controls alone are not a sandbox. Production package execution refuses
  a platform unless actual filesystem and network denial can be established before
  arbitrary package code starts and verified. Existing Windows Job Object and POSIX
  rlimit adapters remain resource primitives, not qualified isolation. No development
  bypass is offered through production configuration. Tests can inject deterministic
  doubles explicitly. Private scratch and bounded child output are required as well.
- H5 gates install, target rebind/rollback and execution admission with live publisher
  trust while preserving historical executable evidence.

## Compatibility and acceptance

Preserve Run/Job/Attempt authority, Stage-D grant cutoffs and audit redaction,
Stage-F immutable context/memory, Stage-G pinning and Stage-I installer handoff.
Schema changes use new local Alembic revisions and synchronized Worker/Scheduler
guards; hosted Marketplace revisions remain independent. Platforms with resource
controls but no qualified isolation now refuse package execution. Built-in trusted
agents continue through their existing execution path.

Acceptance requires key rotation/interruption/retirement, fake-provider OAuth and
refresh races, owner approval/resumption/fencing, actual protected-file and direct
network denial tests for any enabled platform, lifecycle trust regressions and
backend/frontend/migration/static/E2E gates. A deliberately refused platform must
show clear safe user-facing behavior and must never be listed as supported.
