# NervOS Architecture Rules

## Architectural style

NervOS begins as a modular monolith.

Do not introduce microservices, Kafka, Kubernetes, Redis, or distributed
infrastructure unless a later requirement justifies them.

## Core dependency direction

Allowed:

apps/api
    ↓
packages/nervos-core

apps/worker
    ↓
packages/nervos-core

packages/nervos-sdk
    ↓
public NervOS interfaces

Not allowed:

nervos-core → nervos-api
nervos-core → React frontend
domain → infrastructure implementation

## Runtime domain model

Preserve these distinctions:

AgentPackage != AgentInstance
AgentInstance != Run
Session != Run
User != Agent
Memory != Session

Future execution architecture:

Trigger
  ↓
Job
  ↓
Queue
  ↓
Worker
  ↓
Run Coordinator
  ↓
Agent Instance
  ↓
Model / Memory / Tools

Installed agents should normally be idle records.
They must not require a permanent thread/process while inactive.

## Control plane vs execution plane

Control plane:
- API
- dashboard
- auth
- configuration
- registry
- marketplace management

Execution plane:
- scheduler
- queue
- workers
- runtime
- model calls
- tool calls

Keep these concepts separated even while implemented in one repository.

## Persistence

SQLite is the v1 local persistence mechanism.

Keep repositories/services abstract enough that PostgreSQL can be supported later,
but do not build PostgreSQL-specific infrastructure before it is needed.

## Extensibility

Future agent developers should interact with stable NervOS SDK abstractions,
not NervOS internal database models.

Future MCP tools must go through the NervOS tool/permission layer.

Future model providers must go through a model-provider interface.

## Architecture changes

Any significant change to these principles requires an ADR under `docs/adr/`.