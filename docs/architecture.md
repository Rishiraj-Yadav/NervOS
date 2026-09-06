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

Future execution worker entrypoint. It claims jobs and delegates to runtime services.

### `packages/nervos-core`

Primary domain/application code. It must not depend on React or FastAPI route modules.

### `packages/nervos-sdk`

Future public API for third-party agent developers. It should remain smaller and more stable than internal runtime APIs.

### `packages/nervos-mcp`

Future MCP integration and tool-gateway abstractions.

### `packages/nervos-models`

Future model-provider interface and adapters.

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
