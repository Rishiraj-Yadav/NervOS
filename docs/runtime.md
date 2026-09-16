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
POST   /api/v1/runs/{run_id}/cancel
```

There is still no global `POST /runs`, no `POST /chat`, no Run Events endpoint, no worker-health endpoint, no streaming, and no re-execute endpoint.

## Boundaries preserved and changed

- **Ownership.** Identity still comes only from the authenticated session. No request or response schema carries `owner_user_id`, and foreign and nonexistent resources remain indistinguishable.
- **Creatable definition.** Creation remains limited to the exact trusted `nervos.chat@1` definition.
- **Control plane cannot execute.** The API no longer constructs provider SDK clients, no longer stores provider credentials in its settings, and no longer composes a handler registry or `RunCoordinator`.
- **Known provider vs. configured capability.** The API validates that a provider identifier is known. A known provider with no configured Worker credential is accepted into the durable queue and remains queued until a capable Worker exists. Unknown provider identifiers are still rejected.
- **Worker capability filter.** A Worker claims only Jobs whose `jobs.model_provider` is in its configured provider set. A Worker with zero configured providers starts, claims nothing, and fails nothing.
- **No fallback.** There is no provider fallback and no model fallback. The immutable Run snapshot decides the provider/model for execution.

## Queue, fairness, and lease limits

Admission and execution are bounded separately, and the two must not be conflated.

**Admission (backlog) limits** bound how much work NervOS accepts:

- `NERVOS_MAX_PENDING_JOBS` is the global hard admission cap enforced inside the submission transaction.
- `NERVOS_MAX_PENDING_JOBS_PER_AGENT` and `NERVOS_MAX_PENDING_JOBS_PER_PROVIDER` bound one Agent Instance's and one provider's backlog. Both default to the global bound, so neither binds until an operator lowers it.
- All three are counted, and the Run and Job are written, inside the same admission transaction, so a rejection writes no Run, Job, or Event row and two concurrent submissions at the last slot produce exactly one winner. Any of them returns the same generic `429 queue_capacity_exceeded`; which dimension filled is not disclosed, and no queue position, partition state, or count is exposed.

**Execution-concurrency limits** bound how much work runs at once, and since C6 they are authoritative:

- The active predicate is a Job with `status IN ('claimed','running')` **and** `lease_expires_at > now`. A live lease is execution authority, so a Job that is claimed but not yet started still holds its slot. `queued` and `retry_wait` hold no lease and consume no execution capacity.
- A Job is claimable only if the **global**, its **Agent Instance's**, and its **provider's** limits all have room — the intersection, decided at claim time from live Jobs. Nothing is persisted, so no counter can drift after a crash, a recovery, a retry, or a cancellation.
- The limits are one code-level policy (4 global / 4 per Agent Instance / 4 per provider) in `nervos-core`, deliberately not environment settings: each claim compares a database-wide count against its limit, so divergent Worker configuration would raise the effective ceiling instead of being detected.
- `NERVOS_MAX_ACTIVE_JOBS` is **tightening-only**. A Worker's own budget may lower what it claims; it can never raise the authoritative global limit.
- `NERVOS_WORKER_CONCURRENCY` is the number of local execution slots in one process. It is one process's parallelism, never fleet concurrency.
- An expired lease stops consuming capacity for unrelated work, and that is safe: an expired Job is still `claimed` or `running`, so the claim query cannot reach it, and C3 reconciliation remains the only path that returns it to the queue.
- Leases are renewed while a healthy Worker is executing. Every start, heartbeat, success, failure, and finalization retry is fenced on ownership and an unexpired lease.

## Fair selection

C6 replaced global FIFO. Claiming still does not "stop at the blocked head": a partition whose Agent is at its concurrency limit, whose provider is at its limit, or whose provider this Worker cannot run at all is filtered out *before* selection, so it cannot head-of-line block work that this Worker can actually execute.

Among the partitions that remain eligible, the **least-recently-served** one wins, read from durable per-Agent fairness metadata:

```text
never-served Agent partition first   (NULL last_served_attempt_id)
then smallest last_served_attempt_id
then agent_instance_id               (deterministic tie-break)
within that Agent: available_at ASC, then Job id ASC
```

The marker is the `job_attempts.id` of a committed claim, so fairness advances **only when a claim commits**. A poll that finds nothing eligible, loses a capability filter, hits a cap, loses the compare-and-set, or rolls back advances nothing and does not penalize any partition; a partition that was skipped keeps its position and re-enters at it. Fairness is durable across restarts because it is stored, and it is identical for every Worker.

Global FIFO is intentionally no longer the scheduling contract: a newer Job belonging to an Agent whose fair turn has arrived may run before an older Job belonging to an Agent that was just served. Within one Agent, oldest-due still wins.

**The guarantee is bounded, not global.** For a given compatible claiming capability set, continuously eligible Agent partitions cannot be repeatedly bypassed by more-recently-served competitors. Worker capability changes which partitions are eligible at all: a partition no present Worker can run is outside the guarantee, because fairness is not promised for work that cannot execute.

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

Shutdown cancellation is local only: NervOS stops waiting for the provider result and writes no terminal state. It does not claim the remote provider cancelled or rolled back a request it already received. Worker shutdown is **not** owner cancellation — it writes no `cancel_requested_at` and never terminalizes a Run as `cancelled`; a stopping Worker leaves its claim for C3.

## Owner cancellation

Since C5 an owner can cancel their own Run. Cancellation is a **durable control-plane transition, not a message to a Worker**: the API transaction that accepts the request *is* the cancellation. It records `cancel_requested_at` (write-once, never cleared), terminalizes the Attempt, Job, and Run, and appends the events atomically, all before the response returns. Cancellation therefore completes with no Worker running, with a crashed Worker, and against a Job that is merely queued or waiting out a scheduled retry.

Every case is covered:

- **Queued** (`created`) — cancelled before execution. No Attempt is invented and the provider is never invoked.
- **Waiting on a scheduled retry** — cancelled immediately. The due instant stays as append-only history, but no retry can ever execute.
- **Claimed but not started** — cancelled before the execution boundary, so the provider is never called.
- **Running** — cancelled durably. The Worker discovers the revoked authority on its next heartbeat and stops waiting on the local provider task; a late result can never be persisted.

A cancelled Run is its own terminal lifecycle, **not a failure**. It carries no output, no usage, and no provider error. A Run cancelled before execution began has no fabricated `started_at` and a NULL `elapsed_ms`; one cancelled after it began keeps its real `started_at` and reports the truthful interval until cancellation was accepted.

Cancelling appends exactly two events — `cancellation.requested`, then `run.cancelled`. Repeated cancellation is idempotent: the same Run is returned, no second event is appended, and the original finish instant and elapsed interval are preserved. A Run that already **succeeded** or **failed** is history and is never rewritten; cancelling it is refused rather than converted.

**NervOS claims no remote cancellation.** Cancellation revokes *NervOS* authority, asks the local provider task to stop, and guarantees a late result can never be persisted. It does **not** claim the remote provider stopped processing a request it already received, that billing stopped, or that remote side effects were rolled back.

## Attempt execution timeout

Every Attempt has a deadline measured from its own execution boundary. It comes from `provider_timeout_ms` in the Run's immutable limits snapshot and starts only after the durable execution-start commit — queue time, claim time, a scheduled retry wait, and terminal persistence are excluded, and each retry Attempt receives a fresh window.

There are two layers. The inner deadline inside `RunExecutor` bounds a provider call that accepts cancellation. The outer, provider-neutral watchdog in `JobExecutionService` exists because that inner bound cannot complete against a coroutine which suppresses `CancelledError`, and an unanswerable inner bound would let one task hold its slot forever.

Both converge on `model_timed_out`, which is **`AMBIGUOUS`**: a local deadline proves NervOS stopped waiting, never that the provider did not execute the request it already received. A timeout is therefore never replayed, never schedules a retry, and never becomes a cancellation — it terminalizes as a failure. If the provider call has already completed when the deadline becomes ready, its real result is used and a completed call is never relabelled as a timeout.

**Cleanup is bounded, and honestly so.** Python cannot forcibly terminate a coroutine that refuses to die, so C5 guarantees bounded local waiting, revoked durable authority, and no durable resurrection — not that every coroutine disappears. A task that outlives the drain bound is left to finish on its own, tracked until it completes so its eventual result or exception is retrieved and discarded rather than reported as unretrieved. It holds no authority and every terminal write is fenced, so it cannot overwrite or resurrect anything. `WorkerService` shutdown is bounded on the same terms.

## Worker registry and health

Every Worker process incarnation registers a durable row and heartbeats it on an interval; a restart is a new incarnation with a new row, and an old identity is never recycled. Registry health is **derived** from timestamps — `stopped`, `healthy`, or `stale` — and never stored.

Registry health is **observability, not authority**. `stale` means "not observed recently", never "definitely dead": a crashed Worker and a paused one look identical. A stale registry row reclaims nothing, and an expired Job lease is reconciled whether the owning Worker's row looks healthy or stale. A Worker whose own registry row is gone or stopped stops accepting new claims and shuts down in an orderly way; in-flight work keeps its own lease.

## Why is my Run stuck?

- A Run shown as **Queued** (`status="created"`) means it was durably accepted but has not started. Common causes are: no Worker process is running; the running Worker has no credential for that Run's provider; the queue is full for new submissions; or its Agent Instance or provider is at its execution-concurrency limit while other work runs.
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

Conversation sessions, memory, tools/MCP, scheduling, event triggers, package installation, marketplace, and persistent secret management remain unimplemented. A public Run Events API, an event timeline, richer execution observability, and a Worker dashboard remain C7. Provider-side remote cancellation is not implemented and is not claimed. Active-concurrency limits are not runtime-tunable: changing them means changing policy code, and per-Agent-Instance custom limits are not implemented (see ADR 0013).

Two C4 limitations are worth knowing when reading a retried Run. First, `elapsed_ms` and the usage counters describe the **terminal Attempt** only: they exclude earlier Attempts, the retry wait, and total Run wall-clock duration, and there is no cumulative cross-Attempt token accounting. Second, the read-only UI polls for a bounded period and then stops; that bound is not a completion guarantee, because a Run can outlast it through Worker downtime, provider duration, pre-start loss, lease recovery, or host downtime. The Run's durable state is still correct, and reloading the page shows the current state.
