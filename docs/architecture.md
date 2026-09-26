# NervOS Architecture

## Status

This document describes the target architecture. Read `implementation-status.md` to see what is actually implemented.

## Architectural style

NervOS starts as a modular monolith running on one self-hosted node. Components are separated by module boundaries first and split into additional processes/services only when measured operational requirements justify it.

## High-level system

```text
Browser
  |
  v
NervOS Dashboard
  |
  | REST / SSE
  v
API / Control Plane
  |
  +-- Authentication
  +-- Configuration
  +-- Agent Registry
  +-- Package Manager
  +-- Marketplace Client
  |
  v
Execution Plane (later)
  |
  +-- Scheduler/Event Router
  |       |
  |       v
  |      Job
  |       |
  |       v
  +-- Persistent Queue
          |
          v
       Worker Pool
          |
          v
     Run Coordinator
          |
          v
      Agent Runtime
       /    |     \
      v     v      v
   Model  Memory  Tool Gateway
                   |
              Permission Engine
                   |
                   v
              MCP / Tool adapters
```

## Control plane

Responsible for management rather than executing agent work:

- REST API
- dashboard
- authentication
- configuration
- agent/package registry
- package installation
- tool/model connection configuration
- marketplace management
- health/status endpoints

### Implemented API surface today

Beyond health, setup, and authentication, the API currently serves owner-scoped Agent Instance and Run resources:

```text
/api/v1/agent-instances
/api/v1/agent-instances/{agent_instance_id}
/api/v1/agent-instances/{agent_instance_id}/runs
/api/v1/runs/{run_id}
/api/v1/runs/{run_id}/events
/api/v1/runs/{run_id}/cancel
```

Routes depend on **application services** (`AgentService`, the durable submission service, and `ModelProviderCatalog`) resolved from `app.state`. No route module imports SQLAlchemy, `nervos_core.infrastructure`, `anthropic`, or `nervos_models`, and exactly one route calls the durable submission service. The control plane cannot claim, start, heartbeat, or terminalize Jobs.

C2 activated the minimal Worker execution plane, C3 added durable Worker liveness plus expired-lease reconciliation, C4 added the durable safe execution retry engine, C5 added owner cancellation plus Attempt execution-timeout orchestration, C6 added authoritative global/per-Agent/per-provider execution concurrency, durable least-recently-served Agent fairness, and per-dimension admission backpressure, and C7 added public read-only execution observability. C8 added integrated acceptance. Stage D added the tool and MCP layer (D1–D7 complete). Stage E added scheduling, events, and triggers (E0–E5 complete). Stage F added conversations, context, and memory (F0–F5 complete). Installation (Stage G), Marketplace (Stage I), and persistent secret management (Stage H) remain target architecture. Provider-side remote cancellation is not implemented and is not claimed; see ADRs 0012, 0013, and 0014.

## Execution plane

Responsible for performing agent work:

- scheduler
- event router
- persistent queue
- workers
- run coordinator
- runtime
- model router
- memory service
- tool gateway
- permission/approval engine
- artifact/log persistence

## Repository boundaries

### `apps/api`

FastAPI application and HTTP concerns. Routes validate input, resolve authentication, call application services, and map errors. Business logic should not live in route handlers.

### `apps/web`

React dashboard. It never accesses the database directly. Server state lives in TanStack Query; the browser is never the authority for a Run's history and holds no Event it did not receive from the API.

### `apps/worker`

C2/C3/C4/C5/C6/C7 execution Worker entrypoint. It registers its process incarnation in the durable `workers` registry, heartbeats that registration, and reconciles expired Job claims (once at startup and periodically) in addition to claiming eligible queued Jobs — including Jobs whose durable retry instant has arrived — renewing leases while executing, delegating trusted Run execution to core services, and writing terminal Job/Attempt/Run state. A safe execution failure is settled into `retry_wait` rather than a terminal failure, and a due retry is claimed through the same path as queued work, so **retry scheduling involves no separate scheduler process at all**. It has no HTTP surface and never runs Alembic.

**Stage E adds a distinct scheduler process, and it is not this one.** The Stage-E scheduler decides *when* a Run exists — it materializes a trigger occurrence and submits an ordinary Run — and it never claims, executes, retries, recovers or terminalizes anything, holds no provider credential, and exposes no HTTP surface (ADR 0018, ADR 0020). **It is implemented (E0–E5 complete).** Registry health is observability only; the Job lease remains execution authority, as frozen by ADR 0010, and durable retry ownership is frozen by ADR 0011. Its claims, retries, recovery decisions, and terminal writes are what the read-only Run Events surface later reports — the Worker writes; the observability path only reads.

### `packages/nervos-core`

Primary domain/application code. It must not depend on React or FastAPI route modules.

### `packages/nervos-sdk`

Future public API for third-party agent developers. It should remain smaller and more stable than internal runtime APIs.

### `packages/nervos-mcp`

Future MCP integration and tool-gateway abstractions.

### `packages/nervos-models`

Model-provider interface and adapters. It currently holds exactly two implemented production provider adapters: canonical ID `anthropic` through the asynchronous Messages API and canonical ID `openai` through the asynchronous Responses API. Provider-SDK imports are confined to this package; core domain/application remain provider-SDK-free. The package is a future home for further adapters, but only these two reviewed adapters are known today.

## Permanent domain distinctions

```text
AgentPackage
    |
    | install/configure
    v
AgentInstance
    |
    | trigger
    v
Run
```

Conversation grouping is separate:

```text
AgentInstance
  +-- Session A
  |     +-- Run 1
  |     +-- Run 2
  +-- Session B
        +-- Run 3
```

## Concurrency principle

Do not run one permanent thread/process per installed agent. Installed agents are persisted and idle until work exists.

```text
many triggers -> persistent queue -> worker 1 / worker 2 / worker N
```

Default future policy should be one concurrent run per AgentInstance unless the agent explicitly supports safe parallelism.

## Persistence

Stage A uses SQLite + SQLAlchemy + Alembic. SQLite remains a valid single-node runtime store later. PostgreSQL and external queue infrastructure are optional future scale choices, not Stage A requirements.

## Read-only observability boundary

Execution has one direction, and observability does not reverse it.

```text
durable execution writers (submission, claim, start, retry, recovery, cancellation, timeout)
        |
        v
   run_events          append-only, sequenced per Run, structurally free of authority material
        |
        v
owner-scoped read service            ownership reused from the Run's proven rule
        |
        v
GET /api/v1/runs/{run_id}/events     read-only; no mutation verb exists for this subresource
        |
        v
dashboard execution timeline         presentational; the browser is never the authority
```

**Run Events are durable facts, not execution commands.** The observability surface points at
recorded state and never back into claiming, retrying, recovering, cancelling, or any Worker
authority. No route, service, or component in this path can alter what a Job will do next.

**`execution_phase` is a projection, not authority.** It is derived per read from the Job's own
durable status and is never persisted, so it cannot drift; nothing that mutates execution reads it.

**The read surface is safe because the write surface is.** `run_events` has no column for a token,
lease, worker identity, heartbeat, credential, provider payload, prompt, output, or environment
value, and production writers normalize a provider failure to a frozen code before persisting it.
The endpoint performs no response-time redaction, and none is claimed.

**C6's scheduling topology stays private.** Queue position, partition, fairness rank, active and
pending counts, and Worker identity are persistence-internal and are not published anywhere above
the persistence layer.

This boundary is frozen by ADR 0014.

## Architecture invariants

1. Dashboard does not execute agent logic.
2. Marketplace distributes packages; it does not run local user agents.
3. Agent code does not receive raw host credentials by default.
4. Tool access goes through NervOS policy boundaries.
5. Agent memory is scoped and not automatically merged across unrelated agents.
6. Session and Run are separate concepts.
7. Significant architecture changes require ADRs.
8. Observability is read-only: no observability surface may claim, start, retry, cancel, recover, or execute.
