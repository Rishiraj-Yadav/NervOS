# ADR 0012 — Cancellation Authority and Execution Timeout Semantics

Status: Accepted for C5 implementation

## Context

C2 made execution durable, C3 gave Workers liveness and lease authority, and C4 made one class of
provider failure safely replayable. Every one of those milestones can *start* work and *finish*
work, but nothing could **stop** it. A user could submit a Run and watch it, and had no way to say
"not this one", and no Attempt had any bound other than the provider's own cooperation.

Adding "a stop button" is not a UI decision. Stopping execution means deciding who holds the
authority to revoke work that another process is actively doing, what a stopped Run honestly *is*,
and what NervOS may claim about a remote provider it has already called. C4's retry engine sharpens
the problem: a stopped Run must not be reconciled, reclaimed, or retried back into existence.

There is also a second, easily conflated control. An Attempt needs a deadline so one hung call
cannot hold a Worker slot forever. A deadline is not a cancellation: a cancelled Run says the owner
changed their mind, while a timed-out Attempt says NervOS stopped waiting. Collapsing the two would
make "cancelled" a euphemism for "we gave up", and would tempt the retry engine into replaying work
that may already have executed.

## Decision

**Cancellation is a durable control-plane transition, not a message to a Worker.** The API
transaction that accepts a cancellation *is* the cancellation: it writes the request, terminalizes
the Attempt, Job, and Run, and appends the cancellation events atomically, before the HTTP response
returns. A live Worker is therefore never a prerequisite. Cancellation completes with no Worker
running, with a Worker that has crashed, and against a Job that is merely queued or waiting out a
scheduled retry — none of which an acknowledgement-based design could promise.

**The Worker is a follower, and that is the whole safety argument.** Every late write in the engine
is already fenced on a live claim — Job status, owning worker, claim token, and an unexpired lease.
A cancelled Job is terminal, so a stale Worker's success, failure, retry schedule, start, or
heartbeat matches zero rows *without any of those paths knowing cancellation exists*. Nothing had to
be taught about cancellation to make it safe; the fences that protected the claim already did it.

**`cancel_requested_at` is write-once.** The first accepted request is the durable audit fact and is
never overwritten by a later one, and it is never cleared, including by terminalization. Because the
request and the Job's terminal state are written in the same transaction, a Job that carries a
request can never be claimable, which is why the claim query needed no new predicate.

**A cancelled Run is its own terminal lifecycle.** `Run.status='cancelled'` is not `failed` with a
reassuring error code. It carries no output, no usage, and **no provider error at all**, because
refusing to continue is not a provider outcome. Two shapes are legal and they are deliberately
different: a Run cancelled before execution began has no fabricated start boundary and no duration,
while a Run cancelled after it began preserves its real `started_at` and reports the truthful
interval until cancellation was accepted — the same elapsed semantics C3 uses when it closes a
post-start recovery, never a fabricated provider duration. The internal Job does carry a
`execution_cancelled` code, because the frozen Job schema requires every terminal Job to explain
itself; that code is infrastructure-owned, is never produced by a provider, and cannot steer retry
policy. The public Run exposes none of it.

**Order decides, never timestamps.** Cancellation wins only by committing first. A committed
`succeeded` or `failed` is history and is never rewritten into a cancellation, and a committed
cancellation is never rewritten into anything. There is no after-the-fact comparison of wall clocks
anywhere in the design.

**Cancellation prevents every future NervOS obligation.** Queued work is cancelled without inventing
an Attempt and without ever invoking a provider. `retry_wait` work is cancelled immediately, keeping
its due instant as append-only history while making it unreachable. Claimed-but-unstarted work is
cancelled before the execution boundary, so the provider is never called, and the start transaction
and the cancellation are serialized by the same immediate-transaction discipline. Running work is
cancelled durably while the local provider task is asked to stop. A cancelled Job cannot be claimed,
cannot be retried, and cannot be requeued by C3's reconciler, because a terminal Job is not a
candidate for any of them.

### Remote cancellation limit

Cancellation is a promise about **NervOS authority**, and the boundary is stated rather than implied.

NervOS **does** guarantee: NervOS's execution authority over the Run is revoked; the local provider
task is asked to stop; a late result can never be persisted; and no future claim, retry, or
recovery can resurrect the work.

NervOS does **not** guarantee: that the remote provider stopped processing a request it already
received; that billing stopped immediately; or that remote side effects were rolled back. Cancelling
a local wait is not remote cancellation, and the API, the UI copy, and this ADR all say so. No
provider-side cancellation is implemented, and the provider adapters are unchanged.

### Execution timeout

**A deadline is not a cancellation, and the two never share vocabulary.** An Attempt's deadline
comes from `provider_timeout_ms`, already part of the Run's immutable limits snapshot, and it starts
only after the durable execution-start commit. Queue time, claim time, a scheduled retry wait, and
terminal persistence are all excluded, and each retry Attempt receives a fresh window.

C5 owns **two** timeout layers. The cooperative inner deadline inside `RunExecutor` is unchanged: it
bounds a provider call that will accept cancellation. The outer, provider-neutral watchdog in
`JobExecutionService` exists because that inner bound *cannot complete* against a coroutine that
suppresses `CancelledError` — an unanswerable inner bound would let one task hold its slot forever.
Both layers converge on the same normalized outcome: `model_timed_out`, which remains `AMBIGUOUS`.

A timeout is **never replayed and never becomes a cancellation**. A local deadline proves NervOS
stopped waiting; it cannot prove the provider never executed the request it already received, so the
disposition stays `AMBIGUOUS` and can never satisfy C4's retry predicate, which admits exactly one
positively safe code. A timed-out Attempt terminalizes as a failure; it never produces `retry_wait`,
never schedules a retry, and never writes cancellation state.

The watchdog's winner rule is a fact about what already happened, not about scheduling order: if the
provider call has already completed when the deadline becomes ready, its real result is consumed and
a completed call is never relabelled as a timeout.

### Non-cooperative tasks

**Python cannot forcibly terminate an arbitrary coroutine that refuses to die**, and C5 does not
pretend otherwise. What C5 guarantees is narrower and true: **bounded local waiting, revoked durable
authority, and no durable resurrection.**

Orchestration waits for local cleanup only within a fixed bound. A task that outlives it is left to
finish on its own, tracked until it completes so its eventual result or exception is retrieved and
discarded rather than reported as an unretrieved task exception. That is safe rather than merely
tolerable: such a task lost the race for its Attempt, holds no durable authority, and every terminal
write is fenced, so it cannot overwrite, resurrect, or revoke anything. The Worker's shutdown drain
is bounded on the same terms, and shutdown still never writes cancellation state — a stopping Worker
leaves its claim for C3, because operational cleanup is not the owner's decision.

### Milestone boundary

Worker loss and pre-start recovery remain **C3**; positively-safe execution retry remains **C4**;
owner cancellation and Attempt execution-timeout orchestration are **C5**. Queue partitions, fairness,
and stronger backpressure are **C6**, and **C6 owns migration 0006** because C5 took 0005. Public
Run Events, timelines, and Worker/Attempt observability remain **C7**. C5 adds no queue partition, no
`active_attempt_id`, no per-scope concurrency cap, no public execution-observability surface, and no
cancellation reason field.

## Consequences

A user can finally stop what they started, and stopping means something durable: the Run is
cancelled in the database before the request returns, and no Worker, restart, retry, or
reconciliation can undo it.

The costs are explicit. Migration 0005 rebuilds the `runs` table to admit a fifth lifecycle, so
`0001`–`0004` stay frozen while the Run vocabulary grows; a database holding cancelled Runs cannot
be represented by C4's constraints, so the downgrade refuses rather than converting a cancellation
into a failure the user never had. The Worker discovers a revoked authority on its next heartbeat
rather than instantly, so the local task may keep running for up to one heartbeat interval after the
Run is already durably cancelled — the cancellation is immediate, the *stopping* is bounded. A
genuinely non-cooperative coroutine may outlive its drain bound. And cancellation makes no claim
whatsoever about the remote provider.

NervOS still makes no exactly-once guarantee, and C5 adds no remote rollback and no provider-side
cancellation.
