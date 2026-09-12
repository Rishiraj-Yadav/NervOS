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
- **B4 — Provider portability, usage, and final acceptance:** prove the unchanged Chat behavior through a second reviewed adapter, normalize optional usage, and complete architecture/security/test/documentation verification.

Outcome: one trusted agent can execute safely and observably through NervOS without claiming the durable execution engine introduced in Stage C.

## Stage C — Persistent execution engine

Jobs table, persistent queue, worker process, claim/lease semantics, retries, cancellation, concurrency limits, run event log.

Outcome: multiple triggered jobs execute safely without one permanent process per agent.

## Stage D — Tool and MCP layer

Tool registry, MCP gateway/client, capability schemas, permissions, tool audit events, first default tools.

Outcome: agents can perform controlled actions.

## Stage E — Scheduling and events

Cron/interval scheduling, event router, scheduler-to-job flow, webhooks/events.

Outcome: background agents can run autonomously.

## Stage F — Agent sessions and memory

Conversation sessions, messages, session summaries, agent-private memory, user profile memory, retrieval/writing policy, memory UI.

Outcome: persistent useful agent context.

## Stage G — Agent package system

`.nervos` package contract, manifest validation, config schema, dependency install, package verification/signatures, install/update/uninstall/rollback.

Outcome: agents become installable software.

## Stage H — Security isolation

Capability permissions, approvals, encrypted secret manager, sandbox boundary, resource controls, path/network controls, publisher trust.

Outcome: safer third-party execution.

## Stage I — Marketplace

Publisher workflow, metadata, package storage, discovery/search, versioning, one-click local install.

## Stage J — Multi-agent collaboration

Task/handoff contracts, shared workspace memory, orchestrator patterns, explicit inter-agent permissions. Do not automatically merge all agent memories.

## Stage K — IoT

Device registry, ESP32/device gateway, device capabilities as tools, sensor events -> jobs, device permissions.

## Deferred research

Peer-to-peer compute, distributed GPU execution, self-replication, self-modification, large autonomous swarms, and unrestricted autonomous financial actions are not MVP requirements.
