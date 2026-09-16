# ADR 0013 — Queue Concurrency, Fairness, and Backpressure Authority

Status: Accepted for C6 implementation

## Context

C2 gave the engine a durable queue and a single global concurrency check. C3 gave Workers liveness
and lease authority, C4 made one class of provider failure safely replayable, and C5 gave owners a
way to stop work. What remained was the *selection* problem: which Job, out of everything
claimable, should a Worker take next — and how much of the system may any one of its participants
occupy.

Two defects made this urgent rather than cosmetic.

First, **concurrency was not actually authoritative.** The claim compared a database-wide count of
live Jobs against `max_active`, a number each Worker read from its own environment. Since every
Worker enforced its own threshold against the same global count, the fleet's effective ceiling was
the *largest* configured value: a Worker configured generously could lift the limit for everyone,
and no cap existed at all per Agent Instance or per model provider.

Second, **selection was a global FIFO on `(available_at, id)`.** One Agent Instance with a deep
backlog and the oldest Jobs won every claim until it drained. A second Agent Instance, continuously
eligible the entire time, simply waited. That is starvation by construction, not by load.

C6 fixes both, and does so without introducing a scheduler, a leader, a second queue, or a
persisted counter.

## Decision

### Concurrency authority

**Worker-local slots are not fleet concurrency.** `NERVOS_WORKER_CONCURRENCY` bounds how many Jobs
one Worker process runs at once. It says nothing about how much work the *system* may have in
flight, and C6 never uses it as a substitute for a database limit.

**Live Jobs in SQLite are the authoritative record of execution capacity.** Concurrency is counted
at claim time from the Jobs that actually hold authority, and every limit is enforced inside the one
short `BEGIN IMMEDIATE` claim transaction. Nothing is cached, so nothing can drift after a crash, a
recovery, a retry, or a cancellation.

**Three limits compose by intersection.** A Job is claimable only if the global limit, its Agent
Instance's limit, *and* its provider's limit all have room, and only if this Worker is capable of
running that provider. There is no "effective cap" value computed anywhere that could go stale.

**The active predicate is `status IN ('claimed','running') AND lease_expires_at > now`.** A live
lease is execution authority, so a Job that is claimed but has not started yet still holds its slot.
`queued` and `retry_wait` hold no lease and consume no execution capacity.

**Expired authority stops consuming capacity, and that is safe rather than convenient.** An expired
Job is still `claimed` or `running`, so the claim query's source predicate cannot reach it: it is
not directly claimable, and freeing its slot can never produce a second authorized execution of the
same Job. C3 reclamation — CAS-ing the exact expired claim tuple — remains the *only* path that
returns it to the queue, behind its reclaim backoff.

**No active counter is persisted.** There is no increment or decrement path to get wrong, and no
reconciliation step to forget.

### Active policy

The three active limits are one code-level policy object in `nervos-core`, with defaults
**global 4, per-Agent 4, per-provider 4**. They are deliberately *not* environment settings. Each
claim compares a database-wide count against its limit, so two Workers holding different limits
would make the effective ceiling the larger value — divergent configuration would silently raise the
cap rather than be detected. A shared constant cannot diverge, which is the same reason
`LEASE_DURATION` and the C4 retry policy live in the shared library.

The defaults equal the global limit, so they **preserve existing out-of-the-box behavior**: C6 adds
authoritative mechanisms and changes no default throughput distribution. Per-Agent and per-provider
enforcement is real and is proven by tests that inject lower policy values.

`NERVOS_MAX_ACTIVE_JOBS` is retained, but it is **tightening-only**. A Worker's own budget may lower
what it claims; it can never raise the authoritative global limit, because the enforcing comparison
is `min(policy, worker budget)`.

**Runtime-tunable active policy is deferred.** The named alternative — a durable single-row policy
table written by the control plane and read by Workers — would make these limits operator-adjustable
without a code change, at the cost of a migration, two settings, and a fail-closed read path.

### Fairness

**The originally planned global Attempt cursor was rejected during external review, on evidence.**
The design took the Agent Instance of the most recent *global* Attempt as a round-robin cursor. Its
proof assumed a common eligible set across Workers, and Workers do not have one: a Worker that can
only run `anthropic` cannot see an `openai`-only partition. The review produced a counterexample —
Worker X eligible to `{55}`, Worker Y eligible to `{50,60}`. Y repeatedly computes "the first
partition after 55", which is always 60, so Agent 50 is continuously eligible to Y and is never
served. The proof was wrong, not merely incomplete, and the algorithm was replaced.

**Final C6 fairness is durable least-recently-served per Agent partition.** A new table,
`queue_partitions`, stores exactly one fact per Agent Instance: `last_served_attempt_id`, the
monotonically increasing `job_attempts.id` of the most recent committed claim for that partition,
NULL when it has never been served.

Selection is:

1. **Filter** the claimable set by everything that makes work runnable now: legal Job/Run shape,
   due `available_at`, remaining Attempt budget, this Worker's provider capability, and all three
   capacity limits.
2. **Choose the partition** — never-served partitions first (NULL marker), then the smallest
   `last_served_attempt_id`, with `agent_instance_id` as the deterministic tie-break.
3. **Choose the Job** inside that partition by `available_at ASC, id ASC` — oldest due work first.

**Fairness advances only on a committed claim**, because the marker is written by the same
transaction that inserts the Attempt. A poll that finds nothing eligible, loses a capability filter,
hits a cap, loses the compare-and-set, or rolls back advances nothing and penalizes no partition. A
partition that was skipped keeps its position and re-enters at it.

**Global FIFO is intentionally no longer the scheduling contract.** A newer Job belonging to an
Agent whose fair turn has arrived may run before an older Job belonging to an Agent that was just
served. Within a partition, oldest-due still wins.

**No cross-capability global fairness guarantee is claimed.** The honest statement is that, for a
given compatible claiming capability set, continuously eligible Agent partitions cannot be
repeatedly bypassed by more-recently-served competitors. A partition that no Worker present can
execute is explicitly outside the guarantee — fairness is not promised for work that cannot run.

### Queue partitions

`queue_partitions` holds durable fairness history and nothing else. It is **not** a second queue,
**not** a copy of Job state, **not** execution authority, and **not** a count. It has no status, no
lease, no claim token, and no active or pending counter. Jobs remain the only durable execution
obligation, and the Job lease plus Attempt token remain the only authority to execute.

`last_served_attempt_id` carries **no foreign key**, deliberately. It is a monotone sequence marker
that is only ever compared, never dereferenced: correctness does not depend on the referenced
Attempt still being meaningful, and an FK would add a RESTRICT edge that made pruning Attempt
history impossible for no scheduling benefit.

Migration `0006_stage_c6_queue_partitions` backfills deterministically from history: one row per
Agent Instance that already has Jobs, marker = that Instance's highest existing Attempt id, or NULL
when it has never been claimed.

Downgrade **drops the table without refusing**, unlike 0004 and 0005. The metadata is derived
scheduling state, so removing it rewrites no execution history, and a later re-upgrade reconstructs
it from the same deterministic backfill.

### Backpressure

Admission gains two dimensions alongside the existing global bound: a per-Agent Instance pending
limit and a per-provider pending limit. All three are counted, and the Run and Job are inserted,
inside **one admission transaction** — so a rejection can never leave a partially created Run, and
two concurrent submissions at the last slot produce exactly one winner.

The point of the dimensions is that one Agent Instance's or one provider's backlog cannot consume
the whole admission allowance and refuse every other submitter, once an operator tightens them. The
defaults equal the global limit, so neither binds until it is lowered.

Public rejection is unchanged and generic: **429 `queue_capacity_exceeded`**, with the existing safe
message. C6 does not disclose which dimension filled, the queue position, partition state, or any
count.

### No central scheduler

Workers keep pulling. The entire claim decision — capability filter, capacity check, fair selection,
compare-and-set, Attempt insert, fairness advance — happens inside one short `BEGIN IMMEDIATE`
transaction. There is **no leader election, no scheduler daemon, no queue dispatcher, and no cron**,
and C6 adds no new Run, Job, or Attempt status and no new Run Event type. Work that cannot run yet
simply remains `queued` or `retry_wait`.

## Consequences

* Concurrency limits are authoritative for the first time: they are enforced against live state in
  one serialized transaction, and no divergent Worker configuration can raise them.
* Fairness is durable, restart-safe, and identical for every Worker, at the cost of one small table
  and one upsert per claim.
* A blocked partition — saturated Agent, saturated provider, or a provider this Worker cannot run —
  is skipped rather than blocking the queue head, which is the substantive behavioral change from
  global FIFO.
* The fairness query reads a bounded relation and never touches `job_attempts`, so claim cost does
  not grow with the system's execution history.
* The active limits are not runtime-tunable. Changing them means changing policy code, and the
  operator-facing surface for queue behavior remains the admission settings plus the per-process
  Worker knobs. This is a deliberate trade of flexibility for the guarantee that no two Workers can
  disagree about a global cap.
* The `Worker` refuses to run against a schema it does not expect, so an upgrade from 0005 to 0006
  is a normal self-hosted migrate-then-restart transition. C6 promises no mixed-revision rolling
  operation during that window.

## Milestone boundary

Worker loss and pre-start recovery remain **C3**; positively-safe provider retry remains **C4**;
owner cancellation and Attempt execution-timeout orchestration remain **C5**; execution concurrency,
durable Agent fairness, blocked-head skipping, and admission backpressure are **C6**, which owns
migration `0006`. C6 adds no queue-position API, no public Run Events API, no Attempt or Worker
observability surface, no execution timeline, and no SSE or streaming — those remain **C7**. Broad
integrated stress, restart, and load acceptance remain **C8**. C6 adds no runtime-tunable active
policy, no per-Agent-Instance custom limits, no scheduler, no leader, and no preemption of running
Jobs.
