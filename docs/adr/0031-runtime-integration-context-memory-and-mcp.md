# ADR 0031 — Runtime integration: package MCP, context and private memory

Status: Accepted for implementation by explicit user approval on 2026-10-03.

## Authority

The user approved `docs/runtime-integration/priority-1-plan.md`, including changes to previously
accepted stages, while reserving Stage H for the next milestone. This decision extends ADRs
0022–0026 and the frozen Stage-F/Stage-G contracts; it does not silently replace their historical
requirements. Stage-F plan blob: `62cfc0a8039e233c82e2a30ff3fd59495e99395d`.

## Decision

Keep the ordinary Run → Job → Attempt → Worker authority. Capture independent-run memory context
at acceptance without manufacturing a Conversation. Conversational snapshots remain unchanged
and retries never retrieve mutable live memory. Structured SDK context is additive; legacy
rendered input remains compatible. New packages explicitly select context protocol 1.

Reserve the versioned manifest extension `x-nervos-runtime-integration` with `version: 1`.
It declares structured context and portable MCP aliases (upstream name and canonical input-schema
SHA-256), independently of V1 built-in `tools.required/optional`. Bind aliases locally to exact
owner-owned definitions; snapshot bindings with the Run. Invocation uses the existing registry,
source synchronization, grant cutoff, live permission evaluator and durable mediator. A binding
grants nothing. Schema drift and revocation deny dispatch. Installing a package creates no grants.

Memory policy defaults to manual. The owner can explicitly enable review or automatic-private
mode. Packages return at most six facts, each at most 2,000 UTF-8 bytes, through a typed result
contract; total candidate bytes are at most 8,000. The Worker persists eligible candidates with
its fenced successful terminal transaction. Only authoritative conversational completion can
materialize facts. Runtime memory services validate owner/instance, live policy, source provenance,
quotas and idempotency. USER facts always require owner review. Private automatic facts append;
they do not overwrite user-authored facts. Memory remains untrusted data, never permission.

An optional host-owned extractor uses ordinary Runs/Jobs, explicitly enabled per instance, with
bounded input/output and no recursive extraction. Its obligations are durable and recoverable;
extraction or memory failure never changes the primary Run's terminal outcome. Late/cancelled
results cannot retain memory. Disabling policy prevents pending automatic writes.

Add safe owner-scoped tool/grant/audit APIs and authenticated health projections. This extends
ADR 0014 with a separate health route, leaving Run events unchanged: publish observed aggregate
availability and owner workload counts, never worker identity, heartbeat records, claim/lease
authority, foreign workloads or queue topology.

## Consequences

An additive local Alembic migration is required. Installed artifacts remain immutable. Existing
SDK/host environments must negotiate supported features; missing support cannot be silently
presented as structured-context support. Historic snapshots may retain deleted memory content.
No Stage-H encryption, OAuth, account-action approvals or hostile-code sandbox is implemented.
