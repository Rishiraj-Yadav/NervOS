# NervOS Runtime

## Status

Stage C2 asynchronous submission and minimal durable Worker execution is implemented. Stage B's trusted `nervos.chat@1` behavior and the two reviewed production provider adapters remain the only executable agent/model surface, but execution is no longer awaited inside the API process.

## C2 durable execution path

The implemented C2 path is:

```text
authenticated owner + explicit Agent Instance intent
  -> exact nervos.chat@1 definition and known-provider validation
  -> one BEGIN IMMEDIATE submission transaction
  -> committed Run(status=created) + Job(status=queued) + run.created/run.queued events
  -> HTTP 202 Accepted
  -> separate Worker capability-aware claim
  -> committed Attempt(status=claimed) + attempt.claimed event
  -> committed execution-start boundary (Run/Job/Attempt running + attempt.started event)
  -> one bounded ModelCompletion.complete invocation outside any database transaction
  -> heartbeat-renewed lease while the provider call and finalization run
  -> committed terminal Attempt + Job + Run and terminal Run Event(s)
```

`POST /api/v1/agent-instances/{agent_instance_id}/runs` returns **202 Accepted** only after the durable submission transaction commits. The response body is the persisted `RunResponse` with `status="created"`, and `Location` points at `/api/v1/runs/{run_id}`. A later model failure is Run state, not an HTTP failure for the accepted submission.

## Public HTTP surface

Implemented resources remain deliberately narrow:

```text
GET    /api/v1/health
POST   /api/v1/setup
GET    /api/v1/setup/status
POST   /api/v1/auth/login
GET    /api/v1/auth/session
POST   /api/v1/auth/logout
GET    /api/v1/agent-instances
POST   /api/v1/agent-instances
GET    /api/v1/agent-instances/{agent_instance_id}
PATCH  /api/v1/agent-instances/{agent_instance_id}
POST   /api/v1/agent-instances/{agent_instance_id}/runs
GET    /api/v1/agent-instances/{agent_instance_id}/runs
GET    /api/v1/runs/{run_id}
```

There is still no global `POST /runs`, no `POST /chat`, no Run Events endpoint, no cancellation endpoint, no worker-health endpoint, no streaming, and no re-execute endpoint.

## Boundaries preserved and changed

- **Ownership.** Identity still comes only from the authenticated session. No request or response schema carries `owner_user_id`, and foreign and nonexistent resources remain indistinguishable.
- **Creatable definition.** Creation remains limited to the exact trusted `nervos.chat@1` definition.
- **Control plane cannot execute.** The API no longer constructs provider SDK clients, no longer stores provider credentials in its settings, and no longer composes a handler registry or `RunCoordinator`.
- **Known provider vs. configured capability.** The API validates that a provider identifier is known. A known provider with no configured Worker credential is accepted into the durable queue and remains queued until a capable Worker exists. Unknown provider identifiers are still rejected.
- **Worker capability filter.** A Worker claims only Jobs whose `jobs.model_provider` is in its configured provider set. A Worker with zero configured providers starts, claims nothing, and fails nothing.
- **No fallback.** There is no provider fallback and no model fallback. The immutable Run snapshot decides the provider/model for execution.

## Queue and lease limits

- `NERVOS_MAX_PENDING_JOBS` is a global hard admission cap enforced inside the submission transaction. A genuine cap breach returns `429 queue_capacity_exceeded` and writes no Run, Job, or Event row.
- `NERVOS_MAX_ACTIVE_JOBS` is a node-wide active execution cap enforced inside the claim transaction.
- `NERVOS_WORKER_CONCURRENCY` is the number of local Worker execution slots.
- Leases are renewed while a healthy Worker is executing. Every start, heartbeat, success, failure, and finalization retry is fenced on ownership and an unexpired lease.

The pending cap is intentionally global in C2: one owner's backlog can refuse another owner's submission. Per-owner or per-Agent fairness is a later milestone.

## Failure, retry, and recovery truth

C2 records one Attempt per execution and records a `RetryDisposition` for failed Attempts, but it **does not retry execution** and never writes `retry_wait`. Persistence-finalization retry replays only the terminal database transaction; it never invokes the provider again.

C2 has **no crash recovery**. Queued Jobs are durable and can be claimed by any capable Worker. A Job already `claimed` or `running` when its Worker dies remains stranded in that state until a later recovery milestone reconciles it. Once its lease expires it no longer consumes active capacity, but C2 does not requeue it and does not replay it.

Shutdown cancellation is local only: NervOS stops waiting for the provider result and writes no terminal state. It does not claim the remote provider cancelled or rolled back a request it already received.

## Why is my Run stuck?

- A Run shown as **Queued** (`status="created"`) means it was durably accepted but has not started. Common C2 causes are: the Worker process is not running; the Worker is running with no credential for that Run's provider; or the queue is full for new submissions.
- Check the Worker startup log for its configured provider identifiers. If the list is empty or does not include the Run's `model_provider`, that Worker will leave the Job queued.
- A Run shown as **Running** that never finishes usually means the Worker stopped or lost authority after the execution-start boundary. C2 will not recover or retry it; a later recovery milestone owns that.
- A pre-C2 legacy Run that was `created` with no Job can remain permanently `created`; C2 does not fabricate a start time to close it.

## Legacy closeout

C2 includes an explicit operator command for old Stage B data only:

```text
uv run python -m nervos_worker --reconcile-legacy-runs
```

It closes legacy `running` Runs that have no Job as `failed` with `execution_outcome_ambiguous`, using the Run's real `started_at`. It does not run the model, does not create Job/Attempt/Event rows, and leaves legacy `created` Runs untouched. Normal Worker startup does not run this closeout.

## Still not implemented

Conversation sessions, memory, tools/MCP, scheduling, event triggers, package installation, marketplace, cancellation, retry scheduling, worker registry/health, fairness, streaming, and persistent secret management remain outside C2.
