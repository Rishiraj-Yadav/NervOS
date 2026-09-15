# NervOS Runtime

## Status

Stage C3 Worker registry, expired-lease reconciliation, and fencing hardening are implemented, and C4 adds the durable execution retry engine. Stage B's trusted `nervos.chat@1` behavior and the two reviewed production provider adapters remain the only executable agent/model surface, but execution is no longer awaited inside the API process, a Worker's work is recovered or closed truthfully instead of staying stranded, and a failure the provider positively declined is retried durably rather than lost.

## Durable execution path

The implemented path is:

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

The Worker records one Attempt per execution and records a `RetryDisposition` for failed Attempts. Since C4 it replays **exactly one** failure class: a normalized rate limit (`model_rate_limited`), which is evidence that the provider declined the request. Every other failure — a timeout, an unavailable transport, an internal failure, or anything unrecognized — stays terminal and is never replayed, because its remote outcome cannot be excluded.

A safe failure settles like this:

```text
model_rate_limited
  -> the current Attempt becomes failed evidence (SAFE_TO_RETRY, the original error)
  -> the Job moves to retry_wait
  -> available_at stores the durable due instant
  -> claim authority is released
  -> the Run stays running
  -> a compatible Worker may claim it once due
  -> a fresh Attempt and a fresh claim token execute it
```

The backoff is **1 second, then 2, then 4, capped at 4**, with no jitter and no provider-supplied `Retry-After`. `available_at` is an earliest-eligibility boundary, not an appointment: the retry runs when a compatible Worker next polls. Nothing else carries the retry — there is no scheduler process or timer row — so a committed retry survives a restart and needs no recovery.

If the retry budget is exhausted, the Attempt, Job, and Run terminalize with the original `model_rate_limited` and its safe message. There is no separate `retry_exhausted` code: the recorded history shows that the retry could not be scheduled.

Persistence-finalization retry replays only the fenced database transaction; it never invokes the provider again, so a database error can never cause a second model request.

C3 reconciles expired claims automatically, and the authority for that is the **Job lease**, never the Worker registry. Reconciliation selects a Job whose `lease_expires_at` has passed, re-reads its active Attempt, and re-checks the exact claim tuple before mutating:

- **Pre-start loss** (`execution_started_at IS NULL`) — the execution-start boundary provably never committed, so nothing external happened. The Attempt becomes `expired` with `SAFE_TO_RETRY` and the Job returns to the queue (after a short fixed delay, so a Worker that crashes at the same point every time cannot spin). The Run stays `created`, and a later claim receives a fresh token.
- **Pre-start loss with the claim budget exhausted** — the Job and Run are closed as `failed` with `worker_recovery_exhausted`. Execution never began and no model request was issued for those Attempts, so the Run carries **no** `started_at` and **no** `elapsed_ms`. This is an infrastructure outcome, not cancellation.
- **Post-start loss** (`execution_started_at IS NOT NULL`) — a provider call may or may not have been issued, so the outcome is irreducibly **ambiguous**. The Attempt becomes `expired` with `AMBIGUOUS`, and the Job and Run fail with `execution_outcome_ambiguous` using the Run's real `started_at`. It is never replayed.

An expired lease is authority loss: the old Worker may not heartbeat, start, terminalize, or requeue, and every one of its writes is fenced on ownership *and* an unexpired lease. Reconciliation runs once at Worker startup and periodically afterwards, needs no configured provider, and is safe to run in any number of Workers at once — concurrent reconcilers are one-winner, with no leader election.

NervOS makes **no exactly-once guarantee**. Recovery reduces stranded work and removes blind replay; it does not promise a request reached the provider exactly once.

Shutdown cancellation is local only: NervOS stops waiting for the provider result and writes no terminal state. It does not claim the remote provider cancelled or rolled back a request it already received.

## Worker registry and health

Every Worker process incarnation registers a durable row and heartbeats it on an interval; a restart is a new incarnation with a new row, and an old identity is never recycled. Registry health is **derived** from timestamps — `stopped`, `healthy`, or `stale` — and never stored.

Registry health is **observability, not authority**. `stale` means "not observed recently", never "definitely dead": a crashed Worker and a paused one look identical. A stale registry row reclaims nothing, and an expired Job lease is reconciled whether the owning Worker's row looks healthy or stale. A Worker whose own registry row is gone or stopped stops accepting new claims and shuts down in an orderly way; in-flight work keeps its own lease.

## Why is my Run stuck?

- A Run shown as **Queued** (`status="created"`) means it was durably accepted but has not started. Common causes are: no Worker process is running; the running Worker has no credential for that Run's provider; or the queue is full for new submissions.
- Check the Worker startup log for its configured provider identifiers. If the list is empty or does not include the Run's `model_provider`, that Worker will leave the Job queued.
- A Run shown as **Running** that never finishes means the Worker stopped or lost authority after the execution-start boundary. That Run is reconciled to `failed` with `execution_outcome_ambiguous` once its lease expires and a Worker is running to reconcile it; it is never replayed. Check the Worker log for `claim_reclaimed` lines.
- A Run that ends `failed` with `worker_recovery_exhausted` never started at all: its Job's claim budget was exhausted by repeated Worker loss before execution began. Redeploy the Worker and submit a new Run.
- A **pre-C2** legacy Run that was `created` with no Job can remain permanently `created`; NervOS does not fabricate a start time to close it.

## Legacy closeout

An explicit operator command handles old Stage B data only:

```text
uv run python -m nervos_worker --reconcile-legacy-runs
```

It closes legacy `running` Runs that have no Job as `failed` with `execution_outcome_ambiguous`, using the Run's real `started_at`. It does not run the model, does not create Job/Attempt/Event rows, and leaves legacy `created` Runs untouched. Normal Worker startup does not run this closeout.

## Still not implemented

Conversation sessions, memory, tools/MCP, scheduling, event triggers, package installation, marketplace, and persistent secret management remain unimplemented. Cancellation and execution-timeout orchestration remain C5 (and `jobs.cancel_requested_at` is still dormant: nothing reads or writes it). Fairness, queue partitions, and per-Agent or per-provider concurrency remain C6. A public Run Events API, an event timeline, richer execution observability, and a Worker dashboard remain C7.

Two C4 limitations are worth knowing when reading a retried Run. First, `elapsed_ms` and the usage counters describe the **terminal Attempt** only: they exclude earlier Attempts, the retry wait, and total Run wall-clock duration, and there is no cumulative cross-Attempt token accounting. Second, the read-only UI polls for a bounded period and then stops; that bound is not a completion guarantee, because a Run can outlast it through Worker downtime, provider duration, pre-start loss, lease recovery, or host downtime. The Run's durable state is still correct, and reloading the page shows the current state.
