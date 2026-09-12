# NervOS Runtime

## Status

Target architecture. Stage A contains no agent runtime. B0 freezes the planned Stage B trusted-agent proof described in ADR 0007; B1–B4 runtime behavior is not implemented yet.

## Stage B trusted-agent proof

Stage B deliberately proves only:

```text
authenticated owner
  -> explicit trusted Chat Agent Instance
  -> persisted Run
  -> exact-version built-in handler
  -> application-owned model port
  -> selected provider adapter
  -> one bounded non-streaming model call
  -> persisted succeeded or failed Run
```

### Definition, instance, and Run

A trusted built-in Agent Definition is reusable behavior identified by the exact pair `(agent_key, agent_definition_version)`. Stage B provides one definition, `("nervos.chat", "1")`. There is no silent latest-version substitution.

An Agent Instance is user-owned persisted configuration pinned to one exact definition version. It has its own primary-key identity; multiple instances may use the same definition or display name. Instances are created only by explicit user action. An enabled instance is eligible for a new Run but does not consume resources while idle.

A Run is one execution of one instance. It snapshots exact definition key/version, provider, bounded opaque model identifier, input, and effective limits when created. Later instance changes affect future Runs only. Disabling an instance prevents new Runs and does not cancel a Run already executing.

Stage B Chat is one-shot: prior Runs are displayable history, not model context. There are no conversation/message tables or long-term memory.

### Agent and model boundary

The trusted Chat handler uses a small internal async Protocol and an explicit built-in registry keyed by exact definition key/version. The handler owns its fixed versioned system behavior, validates one prompt, constructs one narrow request, performs exactly one model call, and validates bounded text output.

The provider-neutral request carries only system instruction, user text, bounded opaque model identifier, maximum output tokens, and timeout/deadline information. The response carries bounded text, provider/model identity, an optional portable finish reason, and optional normalized token usage. Provider adapters translate this shape internally. Agent behavior never receives a provider credential or SDK object.

`FIRST PROVIDER DECISION REQUIRED BEFORE B2`. B1 remains provider-neutral and uses fakes. Stage B provider credentials are process/environment-only, adapter-only, never persisted, never returned to the browser, never given to Agent behavior, and never logged. No provider-specific variable name is defined before provider selection.

### Proof limits

Initial Stage B limits are:

- request body: 16 KiB;
- user input: at most 8,000 UTF-8 bytes and 4,000 Unicode code points;
- model/persisted output: at most 32,000 UTF-8 bytes and 16,000 code points;
- provider deadline: 60 seconds;
- requested maximum output: 1,024 tokens when supported;
- model calls/agent steps: exactly one;
- history: default 20, maximum 50;
- safe error message: at most 512 code points.

Provider limits may be stricter. Effective limits are part of the immutable Run snapshot. An adapter must fail rather than silently ignore a required bound it cannot honor.

### Lifecycle and persistence boundaries

Stage B uses exactly:

```text
created -> running -> succeeded
                   -> failed
```

Created and running are non-terminal. Succeeded and failed are terminal and immutable. Expected-state conditional writes reject invalid transitions.

The awaited API-process proof runner uses three short transactions:

1. validate the owned enabled instance, capture its immutable snapshot, create `created`, and commit;
2. transition `created -> running`, and commit;
3. await one model call with no database transaction open, then persist `running -> succeeded|failed`, and commit.

A process or persistence failure can strand a Run in its last safely committed `created` or `running` state. This is accepted Stage B behavior. There is no reconciliation, watchdog, lease, heartbeat, retry, recovery worker, stale-run cleanup, cancellation, or streaming.

### Failure classes

- **Pre-run rejection:** invalid input, disabled instance, unavailable definition version, invalid configuration, missing provider configuration, or known unsupported configuration. No Run exists and no model call occurs; return a safe 4xx/configuration error.
- **Execution failure:** a Run exists and execution began, but the provider or bounded execution fails. Persist `failed` with a stable safe code/message when persistence succeeds; the failed resource remains inspectable.
- **Persistence/platform failure:** NervOS cannot create or transition the Run. Return a safe service-unavailable response where possible; do not promise a persistence error can be stored in the failing subsystem.

A model answer is never returned or represented as successful unless its terminal succeeded Run was safely persisted. A successfully persisted failed Run is an HTTP resource outcome, not model success, and should not be converted automatically to HTTP 500.

## Future durable runtime

Stage C implements the accepted ADR 0004 target:

```text
Trigger
  -> Create Job
  -> Persistent Queue
  -> Worker claims job
  -> Create/load Run
  -> Run Coordinator
       - load AgentInstance
       - load Agent Definition
       - load configuration and ownership policy
       - execute from immutable Run snapshot
  -> terminal Run state and append-only events
```

Stage C owns jobs, attempts, workers, claims/leases, retries, cancellation, concurrency, crash recovery, stale-run handling, and run events. Installed instances remain persisted and idle when no work exists.

Later stages add tool/permission boundaries, scheduling/events, conversation sessions and memory, `.nervos` package lifecycle, dashboard management, security isolation, Marketplace distribution, and multi-agent behavior. None is current Stage B behavior.
