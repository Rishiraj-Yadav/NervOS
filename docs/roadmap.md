# NervOS Roadmap

## Stage A — Foundation

Repository/workspaces, FastAPI, SQLite, SQLAlchemy/Alembic, configuration, first-run setup, authentication, React dashboard shell, tests, CI (delivered by milestone A6).

Outcome: secure local web application foundation.

## Stage B — Trusted-agent runtime proof

Stage B proves one trusted built-in, one-shot Chat Agent through a provider-neutral model boundary, a real separately selected provider, bounded awaited execution, persisted success or explicit failure, and a minimal authenticated dashboard interaction. ADR 0007 freezes the deliberately narrow scope and its compatibility with later stages.

- **B0 — Scope freeze and trusted-agent architecture:** define the Agent Definition → Agent Instance → Run boundary, exact version identity, ownership, four-state lifecycle, model port, process-only secret policy, proof limits, failure semantics, and strict exclusions. Documentation/governance only.
- **B1 — Agent-instance and run domain/persistence:** implement the two-table domain, exact-version built-in registry, immutable snapshots, transitions, ownership, Alembic migration, and deterministic fake model boundary. No provider SDK, API, or UI.
- **B2 — Model adapter and bounded proof runner:** after a separately reviewed first-provider decision, implement one real adapter, process-environment credential boundary, and one-call awaited in-process coordinator with short database transactions.
- **B3 — Trusted Chat Agent API and minimal UI:** add owner-scoped resource APIs and a small authenticated one-shot Chat experience whose persisted result survives reload; deterministic CI/E2E continues to use a fake provider.
- **B4 — Provider portability, usage, and final acceptance:** implemented. The unchanged `nervos.chat@1` behavior runs through a second reviewed adapter (OpenAI Responses) behind the same provider-neutral port, usage is normalized per provider, and architecture/security/test/documentation verification is complete.

Outcome: one trusted agent can execute safely and observably through NervOS without claiming the durable execution engine introduced in Stage C.

## Stage C — Persistent execution engine

Jobs table, persistent queue, worker process, claim/lease semantics, retries, cancellation, concurrency limits, run event log.

Outcome: multiple triggered jobs execute safely without one permanent process per agent.

## Stage D — Tool and MCP layer

Tool registry, MCP gateway/client, capability schemas, permissions, tool audit events, first default tools.

The **capability grant model and the call-time permission decision** are owned here: persistent
per-Agent tool grants, held as explicit ALLOW rows and evaluated live before every tool call, together
with the minimal operator-owned trust and egress controls needed to onboard a tool source at all. See
ADR 0015 and ADR 0016.

Outcome: agents can perform controlled actions.

## Stage E — Scheduling and events

Cron/interval scheduling, event router, webhooks/events.

Outcome: background agents can run autonomously.

**E0 — architecture, protocol, and safety freeze — is complete**, and its authority is ADR 0018
(trigger, occurrence and scheduler semantics), ADR 0019 (ingress trust boundary, idempotency and
internal events) and ADR 0020 (Run provenance and the shared acceptance seam). It fixed the division
the whole stage rests on: **Stage E decides *when* a Run exists; Stages C and D continue to decide
*how* it executes.** A schedule, webhook or internal event produces a `TriggerOccurrence`, and that
occurrence submits an ordinary Run through the one existing acceptance seam. There is no
`scheduler-to-job` path that mints execution obligations directly, and Stage E adds no queue, no retry
engine and no second worker.

**E0 through E5 are complete and externally accepted.** Stage E now provides bounded internal events,
owner-scoped trigger management, occurrence history, secure webhook delivery, scheduler recovery,
and integrated acceptance/closeout without adding a second execution path.

## Stage F — Conversations, context, and memory

Conversations, messages, deterministic context assembly, agent-private memory, user profile memory, retrieval/writing policy, memory UI. The canonical plan and frozen architecture are `docs/stage-f/README.md` with ADRs 0021–0023.

**F0 — architecture, protocol, safety, and lifecycle freeze — is complete and externally accepted.** F1 (durable Conversations, Turns, Messages, and Run linkage) is next; no Stage-F runtime feature is implemented yet.

Outcome: persistent useful agent context.

## Stage G — Agent package system

`.nervos` package contract, manifest validation, config schema, dependency install, package verification/signatures, install/update/uninstall/rollback.

Outcome: agents become installable software.

## Stage H — Security isolation

Capability permissions, approvals, encrypted secret manager, sandbox boundary, resource controls, path/network controls, publisher trust.

Stage D lands the capability **grant** model; Stage H deepens it. The division is deliberate:
**Stage D owns static per-Agent grants and the decision made at call time**, while **Stage H owns
interactive per-call approvals (`Ask`/`Approve`/`Deny`), the encrypted secret manager, and real
sandbox, resource and network isolation.** The minimal operator-owned trust and egress controls
Stage D adds to make onboarding possible are replaced and hardened here.

Outcome: safer third-party execution.

## Stage I — Marketplace

Publisher workflow, metadata, package storage, discovery/search, versioning, one-click local install.

## Stage J — Multi-agent collaboration

Task/handoff contracts, shared workspace memory, orchestrator patterns, explicit inter-agent permissions. Do not automatically merge all agent memories.

## Stage K — IoT

Device registry, ESP32/device gateway, device capabilities as tools, sensor events -> jobs, device permissions.

## Deferred research

Peer-to-peer compute, distributed GPU execution, self-replication, self-modification, large autonomous swarms, and unrestricted autonomous financial actions are not MVP requirements.
