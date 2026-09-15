# ADR 0011 — Durable Safe Execution Retry

Status: Accepted for C4 implementation

## Context

C2 made execution durable and recorded a `RetryDisposition` on every failed Attempt, but it
deliberately acted on none of it: every explicit provider failure terminalized the Attempt, Job,
and Run. ADR 0010 then drew the line between Worker liveness and Job-lease authority, and
recorded that provider execution retries — and the question of how crash recovery and execution
retries share the claim budget — were left to C4.

Acting on that evidence is the one place NervOS can *replay an external side effect*, so the
decision is not "add a retry loop". Provider SDKs already retry internally, and the repository
deliberately disables that (`max_retries=0`) because a client-side retry cannot tell whether a
request that timed out was actually processed. C4 must reach the same conclusion at the runtime
level: replay only what the provider positively declined, and treat everything else as
irreducibly uncertain.

## Decision

**NervOS owns execution retry, not the provider SDK.** Retry policy lives in `nervos-core` as a
pure, provider-neutral decision, and is injected explicitly into the execution service. Provider
adapters keep `max_retries=0`, perform no sleeping or retrying, and there is no provider or model
fallback. The schedule is NervOS's, is durable, and is the same for every provider.

**Only positively safe outcomes may be replayed.** Exactly one normalized failure qualifies:
`model_rate_limited`, which is evidence that the request was declined rather than acted on. A
timeout, an unavailable transport, an internal failure, or any unrecognized code is `AMBIGUOUS`
and stays terminal. The write transaction re-derives the same restriction from the exact code as
well as its disposition, so widening the classification map alone cannot widen the replay
surface.

**`retry_wait` plus `available_at` is the durable retry mechanism.** When a safe failure is
settled, the current Attempt becomes immutable failed evidence (`failed`, `SAFE_TO_RETRY`, the
original normalized error, its real start and finish), the Job moves to `retry_wait` with an
absolute due instant in `available_at`, and every claim field is cleared. Nothing else carries
the retry: an in-memory timer, delay row, or scheduler process would be a second source of truth
that a restart could lose.

**No scheduler is required.** A retry becomes eligible when an ordinary Worker poll observes
`retry_wait` with `available_at <= now`, and a due retry is claimed through exactly the same
`BEGIN IMMEDIATE` path as queued work. `available_at` is an earliest-eligibility boundary, never
an appointment: actual execution depends on a compatible Worker polling.

**The Run remains `running` and its start is immutable.** A Run that has begun executing is still
executing while it waits to try again, so C4 adds no public retry status and does not reset the
Run to `created`. `started_at` is the instant execution first began and is never rewritten by a
later Attempt; each retry start asserts the already-`running` shape instead. Read-only projects
only Run state, so retry-wait is invisible to the API by construction.

**Every retry is a fresh claim.** A retry creates a new numbered Attempt with a new 32-byte
claim token and a new lease, and is fenced exactly like a first claim. A previous incarnation's
authority is never resurrected.

**Ambiguity and authority are unchanged.** A crash after the execution-start boundary remains
`AMBIGUOUS` (ADR 0010). A crash *before* a safe failure's `retry_wait` transaction commits
therefore leaves a started Attempt that C3 closes as ambiguous — NervOS declines a legitimate
retry rather than replay a request whose outcome it cannot exclude. Authority loss is likewise
unchanged: a Worker that no longer holds a live lease cannot schedule anything, and its write
affects zero rows. A crash *after* the commit leaves a claimless durable retry that needs no
recovery and survives restarts.

**Database uncertainty never replays a provider call.** Only proven-uncommitted failures
(SQLite BUSY/LOCKED) are replayed, on a fresh connection. Only a read that proves the write did
not commit allows another attempt; an uncertain COMMIT is resolved by reading durable state back
and adopting the committed result. Every path replays at most a database transaction.

**Retry scheduling is anchored once and is replay-stable.** The logical operation captures a
single scheduling anchor when the normalized result exists, and computes the due instant as
`anchor + policy.delay(ordinal)`. The transaction's own clock is used only for lease and fencing
checks. A replayed transition therefore recomputes the identical deadline, which is what lets an
uncertain COMMIT be reconciled against one expected value.

**Backoff is deterministic and the ordinal counts execution failures.** The schedule is 1s, 2s,
then 4s capped, with no jitter and no provider-supplied `Retry-After`. The ordinal counts this
Job's *started* safe execution failures, not its claims: a Worker that died before the
execution-start boundary consumed budget but never called a provider, so it must not slow down a
retry for a request that never happened.

**`max_attempts` remains a shared total committed-claim budget.** Every committed claim consumes
one unit, including a pre-start claim that C3 recovered. A pre-start Worker loss therefore
consumes a slot that a provider retry might otherwise have used. C4 accepts this rather than
introducing a second, hidden counter: the alternative would either delete execution history or
remove the only bound that exists on a Worker that crashes deterministically before every start.
Separating delivery accounting from execution accounting is a future schema decision with its own
review, not a side effect of activating retries.

**Exhaustion is honest.** When the budget is spent, the Job and Run terminalize with the original
normalized error. There is no `retry_exhausted` code: the durable history already shows that the
retry could not be scheduled, and inventing a code would hide which failure actually occurred.

**C4 adds no cancellation, fairness, or observability surface.** Cancellation (C5), fairness and
per-scope concurrency (C6), and public execution observability (C7) remain their own milestones.
`cancel_requested_at` stays dormant, and C4 does not read, write, or prioritize it.

## Consequences

A transient rate limit no longer costs a Run. The failure becomes evidence, the obligation waits
on disk, and any compatible Worker can finish the work after a restart — with no scheduler, no
leader election, and no new schema, because C1 already reserved the state and vocabulary.

The costs are explicit. `Run.elapsed_ms` and Run usage remain the *terminal* Attempt's values, so
a retried Run reports the duration and tokens of the attempt that actually finished rather than a
cumulative total; per-Attempt accounting would require a schema revision. The shared claim budget
means crash recovery and execution retries compete for the same units. And a bounded UI poll
interval is not a completion guarantee for any Run — a long Run's durable state is correct, but
observing it may require a reload until C7 provides richer execution observability.

NervOS still makes **no exactly-once guarantee** and claims no provider-side rollback or
cancellation.
