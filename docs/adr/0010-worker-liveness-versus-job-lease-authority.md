# ADR 0010 — Worker Liveness Versus Job Lease Authority
Status: Accepted for C3 implementation

## Context

C2 made Run submission durable and added a separate Worker process, but deliberately shipped no
reconciler: a Job already `claimed` or `running` when its Worker died stayed stranded forever. C3
closes that gap by adding a durable Worker registry and expired-lease reconciliation.

Adding liveness tracking next to execution introduces a temptation that would be a correctness
defect: treating "this Worker looks dead" as permission to replay its work. A registry row records
what a process *said*, not what it *did*. A crashed process and a long-paused or heavily loaded one
are indistinguishable from timestamps alone, so registry staleness can never be evidence that a
provider call did or did not happen. Recovery therefore needs a different authority, and the
boundary between the two has to be frozen rather than left to each call site.

## Decision

**Worker identity is a process incarnation.** Each Worker process draws a fresh identity at startup
and registers exactly one durable row. A restart is a new incarnation with a new row; an old
identity is never recycled or overwritten.

**The Worker registry is observability, never authority.** It exists so an operator can tell a
stopped incarnation from a crashed one. Health — `healthy`, `stale`, `stopped` — is *derived* from
timestamps and never stored, so it cannot contradict its own evidence. `stale` means "not observed
recently" and never "definitely dead". Registry state never authorizes, blocks, or performs a Job
mutation.

**The Job lease is execution authority.** A Job is reconciled only when its `lease_expires_at` has
passed *and* its exact claim tuple still matches. Registry staleness is neither necessary nor
sufficient: a stale registry row with a live lease reclaims nothing, and a live registry row with an
expired lease does not prevent reconciliation.

**Expired means lost authority.** An expired Worker may not heartbeat, start, terminalize, requeue,
or overwrite. Every owner write is fenced on ownership *and* an unexpired lease, so a late write
from a Worker that lost its claim affects zero rows and is discarded rather than committed.

**Crash classification is driven only by the committed execution-start boundary.** The start
transaction commits before any provider call, so:

- `execution_started_at IS NULL` — the boundary provably never committed. Replay-safe.
- `execution_started_at IS NOT NULL` — a provider call may have been issued. Irreducibly
  **ambiguous** and never replayed.

The window in which the start transaction committed but the process died before issuing the request
is deliberately classified ambiguous. NervOS does not guess.

**Pre-start recovery is a fenced handoff.** The expired Attempt becomes terminal (`expired`,
`SAFE_TO_RETRY`) as evidence, and the Job returns to the queue with a fixed infrastructure backoff
so a deterministic crasher cannot be re-claimed in a hot loop. The Run stays `created`. A future
claim creates a new Attempt with a fresh token. This is crash recovery, not execution retry: it
exists because the execution-start boundary proves nothing external happened.

**Exhausted pre-start recovery is closed truthfully.** A claim budget is consumed by every claim,
including one that never executed, so repeated pre-start loss can exhaust it. When that happens the
Job and Run are closed as `failed` with `worker_recovery_exhausted`, `started_at` NULL, and
`elapsed_ms` NULL. Execution never began and no model request was issued, so no execution timestamp
is fabricated. This is an infrastructure outcome, **not** cancellation.

**Post-start loss is closed as ambiguous.** The Attempt becomes `expired` with `AMBIGUOUS`, and the
Job and Run fail with `execution_outcome_ambiguous` using the Run's real `started_at` and a real
elapsed interval. No output is synthesized and the Run is never replayed.

**Concurrency rests on `BEGIN IMMEDIATE` and CAS.** Reconciliation runs in any number of Workers
with no leader election, no distributed lock, and no coordinator: each mutation is CAS-checked
against the exact claim it classified, so a losing reconciler rolls back having produced no side
effect. A single successful write that finds no live registry row for this incarnation stops new
claims and shuts the Worker down in an orderly way.

**No exactly-once claim.** C3 reduces stranded work and removes blind replay; it does not promise
exactly-once execution.

**Provider execution retries remain C4.** C3 records retry-disposition evidence and never acts on
it: no `retry_wait`, no execution backoff policy, no re-execution after a provider failure.
Cancellation remains C5.

## Consequences

A crashed Worker no longer strands accepted work: pre-start loss is recovered automatically and
post-start loss reaches an honest terminal state. Operators gain a durable record of which
incarnations existed and when they were last seen, without that record ever granting execution
authority — which keeps the failure modes of liveness tracking (clock skew, pauses, partitioning)
from becoming data-loss or duplicate-execution bugs.

The cost is an explicit, narrow schema change: SQLite cannot alter a CHECK constraint in place, so
C3 must rebuild `runs` to admit the never-started `failed` shape. The relaxation is gated on the
`worker_recovery_exhausted` code in both directions, so it cannot leak into ordinary failures, and
the rebuild runs inside `autocommit_block()` with a post-migration `PRAGMA foreign_key_check`
because the migration harness otherwise holds a foreign-key-enabled transaction that would reject
the drop.

The remaining limitation is that the claim budget is shared: pre-start crash recovery and C4's
provider execution retries would draw on the same `max_attempts`. C3 freezes the current accounting
deliberately and records the coupling for C4 to revisit.
