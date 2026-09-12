# NervOS Roadmap

## Stage A — Foundation

Repository/workspaces, FastAPI, SQLite, SQLAlchemy/Alembic, configuration, first-run setup, authentication, React dashboard shell, tests, CI (delivered by milestone A6).

Outcome: secure local web application foundation.

## Stage B — Runtime proof

Introduce AgentPackage, AgentInstance, Run, RunContext, first built-in Chat Agent, model-provider interface, one model adapter/mock, and run persistence.

Outcome: one agent can execute through NervOS.

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
