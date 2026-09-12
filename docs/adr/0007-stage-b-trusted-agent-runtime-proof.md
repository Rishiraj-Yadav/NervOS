# ADR 0007 — Stage B Trusted-Agent Runtime Proof

Status: Accepted for B0 architecture freeze

## Context

Stage A established the authenticated modular-monolith foundation. Stage B must prove that one trusted built-in agent can execute through an application-owned model boundary and leave a durable, inspectable outcome. Durable jobs, workers, recovery, tools, conversation state, memory, package installation, and third-party execution belong to later stages.

ADR 0004 remains the target execution architecture. Stage B needs an intentionally narrower bridge to that target without presenting an awaited API-process call as a durable execution engine.

## Decision

### Scope and identities

Stage B provides one trusted built-in definition identified by the exact pair `("nervos.chat", "1")`. The in-process registry is keyed by `(agent_key, agent_definition_version)`; unknown, unavailable, or duplicate versions fail closed. There is no silent substitution of a latest version.

The concepts remain distinct:

```text
trusted Agent Definition (key + version)
  -> user-owned Agent Instance
  -> immutable-snapshot Run
```

An Agent Instance has its own primary-key identity, owner, bounded display name, enabled state, pinned definition key/version, canonical provider identifier, bounded opaque provider model identifier, and UTC timestamps. Multiple instances may reference the same definition and may share a display name. Setup, login, authentication, and dashboard loading never create an instance implicitly.

Stage B stores only `agent_instances` and `runs`. It has no agent-package, conversation, message, job, attempt, event, secret, or cost table. Stage G owns installed `.nervos` package lifecycle and definition upgrades; its installed definitions can later enter the same conceptual definition registry without changing Agent Instance or Run identity.

### Trusted behavior and model separation

The Chat Agent is trusted NervOS-owned behavior with a fixed versioned system instruction. It validates one user input, constructs one narrow model request, makes exactly one model call, validates a bounded text response, and returns a safe result. It receives no provider credential, SDK object, database session, HTTP request, tool, memory service, filesystem primitive, or arbitrary network capability.

Use an internal lightweight async Protocol/handler and immutable value objects, not an Agent base-class hierarchy. An explicit built-in registry resolves exact definition key/version pairs. These internals are not frozen as public SDK APIs.

Agent behavior calls an application-owned provider-neutral model port. The conceptual request is limited to:

- system instruction;
- user text;
- bounded opaque model identifier;
- maximum output tokens;
- timeout/deadline information.

The conceptual response contains bounded text, canonical provider identifier, actual/declared model identifier, a portable finish reason when available, and optional normalized usage (`input_tokens`, `output_tokens`, `total_tokens`). Provider adapters translate this narrow request internally; Stage B does not expose a general `messages[]` conversation contract.

### Provider and secret boundary

The first provider is not selected in B0:

`FIRST PROVIDER DECISION REQUIRED BEFORE B2`

Selection requires separate review of maintained async Python support, text-generation stability, explicit model selection, timeout/error behavior, testability, usage metadata, dependencies/licensing, operator credential availability, and quota/pricing assumptions. B1 remains provider-neutral and uses fakes.

A Stage B credential comes only from operator process/environment configuration, reaches only its provider adapter, and is never persisted, returned to the browser, given to Agent behavior, or logged. NervOS does not automatically load `.env` files. No provider-specific variable name exists until B2 selects a provider. Stronger long-term secret-management and security hardening belong to the dedicated later security stage; dashboard management remains a separate dashboard-stage concern.

### One-shot execution and immutable snapshots

Stage B Chat is one-shot and stateless. Each user submission creates one independent Run. Prior runs may be displayed as history but are never silently supplied as model context. Conversation sessions, messages, summarization, retrieval, and long-term memory belong to Stage F.

A Run snapshots, at creation, its instance ID, exact agent key/version, provider, opaque model name, bounded input, and effective execution limits. Instance changes affect only future Runs. Disabling an instance prevents new Runs but does not cancel or rewrite an existing Run.

Initial Stage B proof limits are:

- request body: 16 KiB;
- user input: at most 8,000 UTF-8 bytes and 4,000 Unicode code points;
- model/persisted output: at most 32,000 UTF-8 bytes and 16,000 code points;
- provider deadline: 60 seconds;
- requested output: at most 1,024 tokens when supported;
- model calls/agent steps: exactly one;
- run history: default 20, maximum 50;
- safe error message: at most 512 code points.

Provider limits may be stricter. An adapter must not silently ignore a required bound; inability to honor it fails before or during execution. Effective limits are retained in the Run snapshot.

### Run lifecycle and transactions

The only Stage B states are:

```text
created -> running -> succeeded
                   -> failed
```

`created` and `running` are non-terminal; `succeeded` and `failed` are terminal and immutable. Expected-state conditional writes enforce transitions.

Execution uses three short transactions:

1. validate the owned enabled instance, capture the immutable snapshot, insert `created`, and commit;
2. conditionally transition `created -> running`, and commit;
3. with no database transaction open, await one handler/model call; then conditionally persist `running -> succeeded|failed`, and commit.

This runner executes in the API process and the initiating HTTP request awaits it. It is not a queue, background task, worker, or crash-safe engine. A process or persistence failure can strand a Run in its last committed `created` or `running` state. Stage B adds no reconciliation, watchdog, lease, heartbeat, retry, recovery worker, stale-run cleanup, streaming, or cancellation. Stage C owns durable execution, attempts, recovery, concurrency, retries, cancellation, and run events while reusing the application execution boundary.

### Failure semantics

Three failure classes are distinct:

1. **Pre-run rejection:** invalid input, disabled instance, unavailable definition version, invalid configuration, missing provider configuration, or known unsupported configuration. No Run exists and no model call occurs; the API returns a safe 4xx/configuration response.
2. **Execution failure:** a Run exists and execution began, then provider authentication, quota/rate limit, timeout, network availability, malformed response, output bound, or bounded internal execution fails. When persistence succeeds, the Run becomes `failed` with a stable safe code/message and remains inspectable.
3. **Persistence/platform failure:** NervOS cannot create the Run, mark it running, or persist a terminal result. The API returns a safe service-unavailable response where possible. NervOS does not promise that a persistence error can itself be written to the failing store.

A model answer is never returned or represented as successful unless the corresponding `succeeded` terminal Run was safely persisted. A persisted failed Run is a successful API resource operation but not a successful model execution; B3 will finalize whether creation returns 200 or 201 without changing this distinction. Provider failures do not become generic HTTP 500 responses when a failed Run was safely persisted.

### Ownership and persistence

The authenticated server identity supplies instance ownership; browser input never supplies an accepted owner ID. Owner-scoped instance/run lookups return the same not-found result for nonexistent and cross-owner IDs. Tests use multiple synthetic users even though user-management functionality remains absent.

B1's candidate additive schema is subject to its own plan review:

- `agent_instances`: server-generated ID, `owner_user_id` restricted FK, exact agent key/version, non-unique bounded display name, enabled boolean, canonical provider identifier, bounded non-empty opaque Unicode model name, and UTC timestamps. Index ownership lookups; do not impose owner/definition or owner/name uniqueness.
- `runs`: restricted instance FK, four-state lifecycle, immutable input/agent/provider/model/limit snapshot, bounded terminal output or stable safe failure, nullable nonnegative usage, elapsed time, and UTC lifecycle timestamps. Index `(agent_instance_id, created_at)` for recent history.

State-dependent checks keep created/running rows free of terminal fields, succeeded rows free of error fields, and failed rows free of successful output. Alembic remains the sole schema authority; instance deletion is not exposed in Stage B and Run history uses restrictive deletion semantics.

## Consequences

Stage B proves one real, bounded, inspectable execution while preserving clear seams for Stage C durable execution, Stage F conversations/memory, Stage G installed packages, later dashboard management, dedicated security hardening, and Marketplace distribution. The accepted tradeoff is that a process crash can strand created/running rows and an HTTP request remains open during a provider call. The design does not claim process isolation, durable cancellation, retry safety, provider equivalence, or autonomous behavior.
