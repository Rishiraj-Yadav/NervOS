# ADR 0014 — Public Execution Observability and Event Pagination

Status: Accepted for C7 implementation

## Context

By the end of C6 the execution kernel was complete and, to its own user, invisible.

A Run was accepted into a durable Job, claimed under a fenced lease, executed, retried on a
positively-safe rate limit, recovered when a Worker disappeared, cancelled by its owner, timed out
by an outer watchdog, and admitted and executed under three authoritative concurrency dimensions
with durable Agent fairness. Every one of those transitions was durably recorded in `run_events`.
None of it was reachable from the interface: `run_events` was written by six reviewed mutation paths
and read by **no** application service and **no** route.

Three concrete defects made this a milestone rather than a polish item.

First, **there was no Run Events surface at all.** The only reads of the table happened inside
persistence transactions. An owner could see that a Run had reached `succeeded` or `failed`, and
nothing about how it got there — not a retry, not a recovery, not a cancellation request, not a
timeout. All of them were durably recorded and none of them was legible.

Second, **a Run's public status could not express what its Job was doing.** `Run.status` is
`created | running | succeeded | failed | cancelled`, while `Job.status` is
`queued | claimed | running | retry_wait | succeeded | failed | cancelled`. Both `retry_wait` and
active execution present as `running`, so the dashboard could not distinguish "executing now" from
"waiting four seconds to retry", and had to hedge across both cases in one paragraph of copy.

Third, **polling was artificially bounded.** A nonterminal Run stopped being observed after roughly
300 seconds at a 2-second cadence, so a Run waiting on a long provider call, a lease recovery, or a
retry backoff stopped being watched while it was still live. That bound had been recorded in C4 as
an accepted limitation, on the reasoning that an abandoned Run must not be presented as actively
progressing.

C7 closes all three. It is deliberately an **observability** milestone, not an execution one: it
adds one read-only query path and two read-only fields. It changes no lease, no fence, no retry, no
cancellation, no fairness, no cap, and no provider invocation.

## Decision

### The public Event route

**`GET /api/v1/runs/{run_id}/events` is the only new public surface.** It is added to the existing
Runs route module, which is why `router.py` is not modified at all: registering a new router module
would have required relaxing the guards that keep execution-plane vocabulary out of the
control-plane router, and no observability endpoint is worth weakening a boundary for.

The route is a plain authenticated `GET`. It is **read-only by construction** — the module contains
no mutation verb for this subresource, reaches no write primitive, and the application method it
calls performs two `SELECT`s. It cannot claim, start, retry, cancel, reconcile, heartbeat, execute
provider work, or change Worker state.

**Owner scoping reuses the Run's existing, already-tested rule.** The service resolves
`get_run(owner_user_id, run_id)` first, which raises `RunNotFound` for a foreign Run and a missing
one alike. Both therefore produce the same `404 run_not_found` through the one error envelope, so
event history can never be used to probe whether another user's Run exists. Ownership is immutable
once a Run is submitted, so the two-read sequence has no window in which a Run could change hands.

### The public Event projection

The response exposes exactly:

```
sequence, event_type, created_at, attempt_number, code, message, available_at
```

This is an **allow-list**, not a serialization of the stored row, and it is asserted as an exact key
set so a future column cannot leak by default.

Deliberately omitted, each for a stated reason:

* **the global Event `id`** — an AUTOINCREMENT identifier across all Runs. It encodes system-wide
  event volume, and no timeline needs it: `sequence` is the Run-local order.
* **`run_id`** — implicit in the path, and repeating it adds nothing.
* **`job_id` and `attempt_id`** — internal execution-obligation identity. `attempt_number` already
  carries the attempt label a reader needs, and the global identifiers would leak one Run's
  numbering into another's page.
* **worker identity, claim token, lease, heartbeat** — the authority material of the execution
  plane, which observability must never publish.
* **queue and fairness state** — scheduling topology, which C6 keeps persistence-internal.
* **credentials, provider payloads, prompts, and outputs** — none of which the durable Event row
  has a column for in the first place.

`code` and `message` are **retained**, not omitted. They are the sanitized error pair, bounded by
CHECK constraints to 64 and 512 characters, and are already public today on the Run response. They
are exactly what makes a failed attempt legible, and exposing them here is a strictly narrower
surface than the status quo.

### The security boundary

**The durable Event model is already the safe model, and the security guarantee lives in the
writers, not in the reader.**

`run_events` has no column for a token, a lease, a worker identity, a heartbeat, a credential, a
provider payload, a prompt, an output, or an environment value. Nowhere in the schema is there a
JSON or free-form detail column, exactly one insert primitive appends Events, and the free text it
can carry is length-bounded by CHECK constraints rather than by convention. The row is
*structurally* incapable of carrying a secret.

Production writers normalize before anything is persisted. A provider failure is resolved to a
frozen NervOS code, and the persisted message is that code's allow-list entry — a static sentence.
No production call site supplies free text, and the adapters construct their errors from a code
alone. A raw provider exception carrying a key, a private-key marker, or an authorization value is
therefore discarded by the write path.

**There is deliberately no response-time secret-redaction layer, and none is claimed.** The Event
API returns the safe durable value it found; it does not inspect, filter, or rewrite it. A database
that an operator has manually poisoned with arbitrary text is **outside this API's security
guarantee** — the endpoint would faithfully return what was stored, and it is not a control. The
supported guarantee is narrower and stronger: text that reached the row through a production
mutation path is normalized, and the reader has nothing to strip.

### Ordering

**`sequence` is the authoritative order, and `created_at` is display metadata only.**

A Run's sequence is allocated contiguously from a per-Run high-water mark read once inside the
writing `BEGIN IMMEDIATE` transaction, so the stream is `1..N` with no gaps, and a batch appended in
one transaction is committed atomically. `UNIQUE(run_id, sequence)` makes any violation a failed
commit rather than a silent reorder. Several Events legitimately share one `created_at` — a
recovery pair is appended with one instant — which is precisely why nothing orders by time.

### Pagination

Pagination is a **keyset cursor on `sequence`**, never an offset:

```
WHERE run_id = :run_id AND sequence > :after_sequence
ORDER BY sequence ASC
LIMIT :limit
```

`limit` defaults to 50 and is bounded to 1..200; `after_sequence` is `>= 0`; out-of-range values are
rejected by the framework with `422`. The page is wider than a Run *page* because one Run
accumulates a legitimate history where a Run page does not, and bounded because an unbounded page is
an unbounded response.

The envelope carries `next_after_sequence`: the last returned sequence when the page came back full,
and `null` otherwise. A client loop is therefore `after_sequence = next ?? last_seen`, terminating
on `null`. A history that ends exactly on a page boundary costs one further empty request to prove
it is drained, which is preferred to a count query that could not be made atomic with the page it
was asked about anyway.

**Concurrent appends are picked up by later polls, and no snapshot token is required.** One request
observes one committed statement snapshot. Because a writer allocates its sequence from
`MAX(sequence) + k` inside its own transaction, a batch is committed atomically: a reader sees the
whole batch or none of it, never `+2` without `+1`. Consequently, a client that has applied up to
sequence `S` cannot have skipped an Event — everything at or below `S` was returned, and everything
above it is returned by the next request. That is a property of the allocation rule, not a
heuristic.

### No migration

**C7 consumes no migration number and adds no index.** Head remains
`0006_stage_c6_queue_partitions`.

This was measured rather than assumed. `UNIQUE(run_id, sequence)` already materialises in SQLite as
an implicit index, and the pagination predicate plans as a range seek on both of its columns:

```
SEARCH run_events USING INDEX sqlite_autoindex_run_events_1 (run_id=? AND sequence>?)
```

with no temporary B-tree for the sort, no scan of other Runs, and the `LIMIT` satisfied by index
order. An index "added" for C7 would be an explicitly named duplicate of one SQLite already
maintains: pure cost, and a permanently larger reviewed schema.

A consequence worth recording: SQLAlchemy's inspector **does not report SQLite's implicit
autoindexes**, so the migration suite's assertion about the `run_events` index set is unchanged.
C7 asserts the query **plan** instead, which is the property that actually matters.

### Derived Run observability

Run responses gain two additive, read-only fields:

* **`execution_phase`** — the Job's own durable status, verbatim:
  `queued | claimed | running | retry_wait | succeeded | failed | cancelled`. It renames nothing,
  invents no vocabulary, and adds **no new Run status**.
* **`retry_available_at`** — the Job's `available_at`, exposed only while the phase is
  `retry_wait`, which is what makes the retry copy exact rather than vague.

Both are derived per read by joining the one Job behind the Run and are **never persisted**: a
stored copy could only drift. Neither carries authority. Nothing that mutates execution reads them,
the cancellation control remains gated on the Run's own status, and a phase that is stale the
instant it is serialised can hide no action from an owner who is entitled to take it.

### Polling

**Stage C observability is HTTP polling; there is no SSE and no WebSocket.** Sequence-keyed
incremental polling is lossless — the cursor is the last applied sequence — and needs no connection
lifecycle, reconnect, or backpressure design. Streaming remains a possible future optimisation, not
a Stage C outcome.

The Event timeline fetches **only what it has not seen**: the first request is `after_sequence = 0`
and every later request uses the highest sequence applied so far. Pages are merged by a union keyed
on `sequence` alone — never a timestamp, never array position — which is what makes duplicate
delivery render one row and out-of-order responses unable to move the applied cursor backwards. The
client is never the authority for the timeline: it holds no Event it did not receive from the API.

The hard ~300-second polling stop is **retired**, not merely raised. While a displayed Run is
nonterminal the cadence is 2 seconds for the first 30 updates and 10 seconds afterwards, with no
hard stop; once every displayed Run is terminal, polling stops entirely. C4's original rationale for
the bound was that an abandoned Run must not be presented as actively progressing. With a durable
timeline and a truthful derived phase, a Run that is not progressing renders as exactly that, so the
UI no longer needs a request budget to stay honest.

**When a Run becomes terminal, the timeline performs one final catch-up drain cycle.** The interval
has already stopped by then, so one further fetch is issued deliberately; because a fetch drains
every remaining page, that single cycle may span several requests. It ends when the cursor reports
no further page, and polling then stops permanently for that Run. Without this, the terminal Event —
committed in the same transaction that made the Run terminal — could be missed by a timer that
stopped first.

## Consequences

* The execution kernel becomes legible to its owner: retries show their failure and their due
  instant, recovery is distinguished into pre-start and ambiguous, cancellation and timeout are
  visibly different things, and every row is a durable fact rather than an inference.
* Observability is a projection of one Run's own facts and publishes no scheduling topology. Queue
  position, partition, fairness rank, active and pending counts, and Worker identity remain private,
  and the frontend is forbidden from naming them.
* The Run shape changed additively. No field was renamed or removed, the frontend validator ignores
  unknown keys, and the existing exact-body assertions compare a response to itself — so no existing
  client breaks.
* The Event API has no mutation authority and cannot be used to drive execution, so it cannot become
  a second control plane.
* Retry, recovery, and cancellation policy internals stay private. The timeline reports what was
  recorded — never a speculation about why, and never a claim that a Worker "died", because a stale
  registry row and a paused process are indistinguishable by design.
* A timeout is presented as a failure and never as a cancellation, and cancellation copy preserves
  its own limit: NervOS stopped waiting locally, and a request already sent may still have been
  processed.
* The timeline's safety is structural rather than maintained. Choosing the surface whose durable row
  has nothing to redact is what makes the guarantee durable under future edits, and it is why C7
  declined the alternative surface described below.

## Excluded, with reasons

* **No Attempt API.** `job_attempts` is the table that *does* hold fenced execution material: its
  columns include `claim_token`, `worker_id`, `lease_expires_at`, and `last_heartbeat_at`. An
  Attempt endpoint would therefore carry a permanent obligation to project those four columns away
  correctly on every response, forever. The Event timeline represents the whole user-facing attempt
  history — which attempt, when it started and finished, why it failed, and whether recovery
  classified it — from a table with nothing to redact. Choosing the surface with nothing to strip is
  the safer design, not merely the smaller one.
* **No Worker health API and no Worker dashboard.** Registry health is observability of the
  execution plane, not authority, and it is not a Stage C outcome. Exposing it would also require
  narrowing a guard written specifically to keep the registry out of the control plane. If a later
  milestone wants it, the honest form is a read-only projection with `stale` documented as "not
  observed recently", never "dead".
* **No queue-position, fairness-rank, active-count, or pending-count API.** C6 deliberately exposed
  no scheduling topology, and a queue-position promise would be a new correctness contract that
  C6's partition marker is explicitly not designed to make.
* **No SSE or WebSocket.** See Polling.
* **No runtime-tunable concurrency policy and no per-Agent custom limits.** ADR 0013's named
  limitation, unchanged.

## Milestone boundary

**C7 owns** the public Run Events read surface with its projection and pagination, the derived
read-only execution phase, the execution timeline, the polling model and lifetime, and this record.

**C8 owns** integrated deterministic acceptance for Stage C — composition across durability,
recovery, retry, cancellation, timeouts, concurrency, fairness, backpressure, observability and
restart — and the truthful Stage C closeout. C8 adds no product architecture: it changes no
execution semantics and introduces no API, status, table, migration, policy, transport,
configuration, or authority mechanism.

**Deferred, and not begun:** streaming; a public Worker health surface or dashboard; a public Attempt
API; a queue-position API; runtime-tunable concurrency policy; per-Agent-Instance custom limits;
preemption; cumulative cross-Attempt token accounting; and any retention or archival policy for
Event history.

**Stage D** owns the tool registry, the MCP gateway and client, capability schemas, the permission
engine, tool audit events, and the first default tools. It is not started here.
