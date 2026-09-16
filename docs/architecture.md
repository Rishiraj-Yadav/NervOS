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

Beyond health, setup, and authentication, the API currently serves exactly two owner-scoped resources:

```text
/api/v1/agent-instances
/api/v1/agent-instances/{agent_instance_id}
/api/v1/agent-instances/{agent_instance_id}/runs
/api/v1/runs/{run_id}
```

Routes depend on **application services** (`AgentService`, the durable submission service, and `ModelProviderCatalog`) resolved from `app.state`. No route module imports SQLAlchemy, `nervos_core.infrastructure`, `anthropic`, or `nervos_models`, and exactly one route calls the durable submission service. The control plane cannot claim, start, heartbeat, or terminalize Jobs.

C2 activated the minimal Worker execution plane, C3 added durable Worker liveness plus expired-lease reconciliation, C4 added the durable safe execution retry engine, and C5 added owner cancellation plus Attempt execution-timeout orchestration. Installation, schedules, tools, memory, permissions, Marketplace, SDK, fairness, queue partitions, and event streaming remain target architecture. Provider-side remote cancellation is not implemented and is not claimed; see ADR 0012.

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

React dashboard. It never accesses the database directly.

### `apps/worker`

C2/C3/C4 execution Worker entrypoint. It registers its process incarnation in the durable `workers` registry, heartbeats that registration, and reconciles expired Job claims (once at startup and periodically) in addition to claiming eligible queued Jobs — including Jobs whose durable retry instant has arrived — renewing leases while executing, delegating trusted Run execution to core services, and writing terminal Job/Attempt/Run state. A safe execution failure is settled into `retry_wait` rather than a terminal failure, and a due retry is claimed through the same path as queued work, so no scheduler process exists. It has no HTTP surface and never runs Alembic. Registry health is observability only; the Job lease remains execution authority, as frozen by ADR 0010, and durable retry ownership is frozen by ADR 0011.

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

## Architecture invariants

1. Dashboard does not execute agent logic.
2. Marketplace distributes packages; it does not run local user agents.
3. Agent code does not receive raw host credentials by default.
4. Tool access goes through NervOS policy boundaries.
5. Agent memory is scoped and not automatically merged across unrelated agents.
6. Session and Run are separate concepts.
7. Significant architecture changes require ADRs.
