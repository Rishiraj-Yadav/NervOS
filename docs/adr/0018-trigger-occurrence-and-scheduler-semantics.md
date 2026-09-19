# ADR 0018 — Trigger, Occurrence, and Scheduler Semantics

Status: Accepted for E0 architecture freeze

This ADR freezes **when a Run exists**. The trust boundary for the ingress that can cause one is
ADR 0019; the single acceptance path every Run — manual or triggered — travels is ADR 0020.

Read together, these three ADRs are the whole of Stage E's architecture. A future session should need
nothing else to recover it.

## Context

Stages B through D produced a durable execution kernel that is authoritative about *whether* a Run may
execute (a live Job lease and a fenced Attempt token), *how* it may retry (one positively-safe outcome
code), and *what it may touch* (live, call-time capability grants). Every one of those stages assumed
the same thing: **a person pressed a button.** Run submission is an authenticated, origin-checked,
owner-scoped HTTP request.

Stage E is the first time a Run can come into existence with nobody at the keyboard. That single change
is the whole of the milestone's risk, because every safety property C and D built — concurrency caps,
retry policy, cancellation authority, grant cutoffs, no-replay-after-dispatch — becomes load-bearing
without an operator watching. The component most likely to break them is a scheduler, because a
scheduler is exactly the kind of component that grows its own notion of "work to do".

Three facts about the merged repository shape everything below.

**The acceptance seam already exists and is provider-neutral.** `AgentService.submit_run` in
`packages/nervos-core/src/nervos_core/application/agents.py` imports no FastAPI and no HTTP type, and
is already invoked outside the API plane by `scripts/manual_anthropic_b2_proof.py`. Nothing about Run
submission has to be invented; the work is to *join* it, not to duplicate it.

**There is no scheduler, and no scheduling concept, anywhere in production source.** `git grep` for
`trigger`, `schedul` and `webhook` across `packages/*/src` and `apps/*/src` returns zero hits. Stage E
starts from nothing, which means every decision below is a first decision rather than a change of mind.

**A second execution path is structurally forbidden, not merely discouraged.** `jobs` carries
`UniqueConstraint("run_id")`, so one Run can have at most one execution obligation; C6's queue policy is
the only capacity authority; and four architecture guards already exist to keep execution-plane
vocabulary out of the control plane. Stage E must add *callers* of one seam, never a second seam.

The roadmap's outcome line for the stage — *"background agents can run autonomously"* — is the
requirement. This ADR is the smallest architecture that delivers it without giving Stage E any
execution authority at all.

## Decision

### Stage E decides WHEN a Run exists. Stage C/D decide HOW it executes.

```
Schedule  /  Webhook  /  Internal event
                    ↓
            TriggerOccurrence          durable, immutable, uniquely identified
                    ↓
            normal Run submission      the one acceptance seam (ADR 0020)
                    ↓
              Run  +  Job              created together, atomically, as today
                    ↓
                 Attempt
                    ↓
        C3 / C4 / C5 / C6  +  D2 / D4 / D5 / D6
```

A schedule-created Run and a manually-created Run are the **same kind of object** in every respect:
the same immutable Agent snapshot, the same Job, the same queue, the same claim semantics, the same
retry policy, the same cancellation authority, the same concurrency limits, the same provider limits,
the same tool loop, the same grant cutoff, the same `ToolInvocation` ledger, the same
ambiguity-without-replay rules, the same Run Events, and the same terminalization.

Stage E may add a durable fact recording **why** a Run was created. It may not change **how** the Run
is executed.

### What Stage E must never create

`ScheduleJob`, `TriggerJob`, `CronJob`, `WebhookJob`, `EventJob`, `TriggerAttempt`,
`SchedulerAttempt`, a trigger worker, a second execution queue, a second retry engine, or a second
Worker execution architecture.

A trigger's causal chain **terminates at Run creation**. Everything after that belongs to C/D.

### Terminology, fixed so the words cannot drift

| Term | Meaning |
|---|---|
| **TriggerDefinition** | Current operator configuration. The only row a user edits. Configuration authority. |
| **TriggerOccurrence** | The immutable, durable fact that *this trigger became due* or *this delivery/event arrived*. Trigger-materialization history. |
| **Run** | The ordinary NervOS execution. Execution authority. |
| **Job / Attempt** | The ordinary durable execution obligation and its claim episodes. Execution authority. |
| **Disable** | Stop *future* occurrence materialization. Never cancels, mutates, or stops an existing Run. |
| **Cancel** | C5's action on an already-created Run. The **only** Run-stopping authority. |
| **Materialize** | The single transaction that turns a due/received trigger into an occurrence and an ordinary Run. |

Calling a trigger disable "cancelling a Run" is forbidden terminology. A one-time trigger may offer a
"cancel before it fires" product action; internally that is disable, never C5 cancellation.

### TriggerDefinition authority

A `TriggerDefinition` is owned by exactly one user and targets exactly one Agent Instance **that the
same user owns**. There is no cross-owner trigger target and no sharing model in Stage E.

Fields: durable integer primary key; `owner_user_id`; `agent_instance_id`; `kind`; `display_name`;
`enabled`; `input_text`; `config_revision`; the kind-specific configuration; `next_fire_at`; the
webhook locator and secret digest; the event type; and creation/update timestamps.

The five kinds are exactly:

| Kind | Meaning | Timezone |
|---|---|---|
| `one_time` | one absolute UTC instant | none |
| `interval` | a fixed number of UTC seconds between occurrences | **none** |
| `cron` | a 5-field local-calendar expression | IANA name, default `UTC` |
| `webhook` | an authenticated inbound HTTP delivery | none |
| `event` | a published owner-scoped internal event | none |

No further kind is added in Stage E. Filesystem watchers, email and calendar polling, MQTT, Kafka,
message-queue connectors and IoT devices are all expected to publish into the `event` kind rather than
add a kind of their own.

**Schema shape.** One flat table with kind-shaped nullable columns, with the legal shapes pinned by a
single `CHECK` — the same idiom the repository already uses for heterogeneous rows
(`runs.lifecycle_shape`, `jobs.claim_shape`, `job_attempts` shape constraints). Not a JSON config blob,
because SQLite cannot enforce its shape and the repository's one JSON column already needs a
hand-written validator, a digest *and* a size bound to be safe. Not child tables, because no table in
this schema has a child-table-with-discriminator and the table set is a reviewed architecture fact.

### TriggerOccurrence authority

A `TriggerOccurrence` records that a trigger became due, or that something was delivered to it. It is
**written once, with its final status, and never updated.**

It answers one question: *did this trigger create this Run?* It never answers *what happened during
that Run* — Run/Job/Attempt are the sole execution record, and mirroring execution state into an
occurrence would create a second source of execution truth.

Fields: durable primary key; `trigger_definition_id`; `owner_user_id`; `agent_instance_id`;
`trigger_revision`; the kind-specific identity (`nominal_at`, `event_id`, `idempotency_key`); payload
digest and byte count; `status`; `skip_code`/`skip_message`; `run_id`; `occurred_at`; `created_at`.

**`kind` is deliberately NOT stored on the occurrence.** `TriggerDefinition.kind` is immutable, the
occurrence always holds a `RESTRICT` reference to its definition, and a definition cannot be deleted
while history exists — so the kind is always derivable and a stored copy could only ever be a second
version of one fact. Storing it would also invite a `CHECK` that compares an occurrence column against
another table's value, which SQLite cannot express.

**`owner_user_id` and `agent_instance_id` ARE stored**, because they are historical authority: they
record who the trigger targeted *at the moment it fired*, and a later edit of the definition must not
rewrite that.

### The corrected occurrence status vocabulary

Exactly two statuses:

| Status | Meaning | `run_id` | `skip_code` |
|---|---|---|---|
| `run_created` | materialized; an ordinary Run exists | non-NULL | NULL |
| `skipped` | recognised as due/received, but no Run was created, for a safe static reason | NULL | non-NULL |

**There is no `duplicate` status, and a duplicate produces no occurrence row at all.** Reason: the
frozen identity constraints mean a duplicate identity refers to an **already-existing** occurrence.
For

- schedule — `(trigger_definition_id, nominal_at)`
- webhook with an idempotency key — `(trigger_definition_id, idempotency_key)`
- event — `(trigger_definition_id, event_id)`

a repeated delivery or publication must **find and return the existing occurrence**, create no second
occurrence and create no second Run.

An API or application result may carry an **ephemeral** `duplicate: true` (or a typed equivalent) to
tell the caller that it received an identity it had already seen. That response fact is **not
persisted** as another occurrence status, because occurrence history records *materialized
occurrences*, not every duplicate delivery attempt. A history that logged every retry as a row would
misrepresent one event as many.

**There is no `pending` status.** Because occurrence insertion, Run submission, Job creation and future
schedule advancement all commit in one transaction, there is no window in which an occurrence exists
without its outcome — and therefore no durable obligation a second worker would have to sweep. A
`pending` state would be exactly the second execution primitive this architecture forbids.

Occurrence terminality is therefore true by construction: the row is written once, in its final state.

### Occurrence identity, and the at-most-one-Run guarantee

| Kind | Identity | Enforced by |
|---|---|---|
| `one_time` / `interval` / `cron` | `(trigger_definition_id, nominal_at)` | unique index |
| `webhook` with `Idempotency-Key` | `(trigger_definition_id, idempotency_key)` | unique index |
| `webhook` without a key | **none** — every delivery is a distinct occurrence | — |
| `event` | `(trigger_definition_id, event_id)` | unique index |

`nominal_at` is the **scheduled** instant, never the instant the scheduler noticed it. A cron
occurrence due at 09:00 but materialized at 09:07 stores `09:00`. That is what makes identity stable
across restarts, clock skew and long downtime; `occurred_at` separately records when it was recognised.

`trigger_occurrences.run_id` is **UNIQUE**, which is what makes "one occurrence → at most one Run" a
database fact rather than a code convention. SQLite treats `NULL`s as distinct under a `UNIQUE` index,
so the many skipped occurrences that carry no Run coexist correctly with the constraint.

The truthful guarantee is: **one deterministic schedule occurrence produces at most one NervOS Run.**
This is *not* an exactly-once claim about external effects — see ADR 0020.

### One-time semantics

A `one_time` trigger represents **one absolute UTC instant** and can materialize **at most one
occurrence**.

| Situation | Behaviour |
|---|---|
| Representation | `run_at`, an absolute aware UTC instant. `timezone` is NULL — there is no local-time reading. |
| Created in the past | Allowed. The misfire policy governs what happens, not validation. |
| Due while NervOS was offline | Fires **once** on resume. A one-time trigger is the one kind for which a catch-up occurrence is unconditionally correct: there is exactly one nominal instant and it can only ever be honoured once. |
| Durable proof it cannot fire twice | The unique index on `(trigger_definition_id, nominal_at)`. |
| Disabled before firing | `enabled = 0`, `next_fire_at = NULL`. Nothing is lost; `run_at` is still stored. |
| Enabled after its nominal time has passed, with no occurrence yet | Becomes immediately due once. |
| **After its sole occurrence — `run_created` OR `skipped`** | **Terminal.** `enabled = 0`, `next_fire_at = NULL`. The occurrence is permanent proof that its nominal instant was consumed. |
| Re-enabling a completed one-time trigger | **Rejected.** To schedule again, create a new one-time trigger, or edit the existing one *before* it fires. |
| Editing `run_at` after firing | **Rejected.** Editing `display_name` or `input_text` remains allowed but does not retroactively change the fired occurrence's meaning. |
| Duplicate identity | `(trigger_definition_id, nominal_at)` where `nominal_at = run_at`. |

The terminal transition applies to a `skipped` occurrence too: a one-time trigger whose Agent was
disabled at its instant has consumed that instant. It does not fire later, and it does not silently
wait. The skip is visible in history and the user resolves it by creating a new trigger or re-enabling
after editing `run_at`.

### Interval semantics

**Fixed-rate scheduling, anchored to an explicit start instant.** Explicitly **not** "N seconds after
the previous Run finishes" — Run duration must never move the clock. A Run that takes forty minutes
must not shift a five-minute schedule.

| Property | Frozen value |
|---|---|
| Unit | **UTC seconds only.** No local-time arithmetic, no DST interaction. |
| Timezone | **None.** `timezone` is NULL and the API rejects one being set. A user who means "09:00 local every day" must use `cron`, not a 24-hour interval. |
| Minimum | **`INTERVAL_MIN_SECONDS = 60`** — minute granularity, and a hard cap on amplification. |
| Maximum | **`INTERVAL_MAX_SECONDS = 31_536_000`** (365 days). Bounds arithmetic, refuses absurd integer input, and removes the `datetime`/`timedelta` overflow class. Long calendar scheduling belongs to `cron` or `one_time`. |
| Anchor | `next_fire_at`, set at creation to `now + interval` — never to `now`, so enabling a trigger never produces a surprise immediate Run. |
| Next occurrence | `next_fire_at + interval_seconds`, computed in absolute UTC seconds. |
| Missed occurrences | **Coalesced to at most one.** |
| Enable | `next_fire_at = now + interval`, recomputed forward. Never restored from a stale value. |
| Disable | `next_fire_at = NULL`. |
| Edit | `config_revision` increments; `next_fire_at` is recomputed forward from `now`; existing occurrences are untouched. |

### Cron dialect — frozen, and NervOS-owned

| Property | Frozen value |
|---|---|
| Field count | **Exactly 5** — `minute hour day-of-month month day-of-week` |
| Seconds field | **Rejected.** Sub-minute scheduling is the largest amplification risk available, and nothing in the roadmap needs it. |
| Resolution | One minute |
| Supported syntax | integers, `*`, ranges `a-b`, steps `*/n` and `a-b/n`, comma lists, month and day **names** (`JAN…DEC`, `MON…SUN`, case-insensitive) |
| Rejected extensions | `L`, `LW`, `L` in day-of-week, and `#` (nth weekday) |
| Both day fields restricted | Standard OR semantics, matching Debian cron |
| Normalization | The stored expression is the **user's exact string**, never rewritten — a rewritten expression would make `trigger_revision` untrustworthy |
| Bounds | 1…128 bytes, no NUL, non-blank |
| Invalid expression | Rejected at **create/update** time with a safe validation error. Never at fire time. |
| No future occurrence | Rejected at create/update (see below) |

**NervOS's validator is the authority on the accepted dialect, and it accepts a strict subset of what
the evaluation library accepts.** The library may only compute instants from an expression the domain
validator has already accepted, and it is importable only in the infrastructure adapter — never in
`domain/`. This is what keeps the dialect a NervOS fact rather than a library property, and it is why
choosing a third-party evaluator does not widen the contract.

### An enabled recurring schedule must always have a valid next fire time

An enabled `interval` or `cron` trigger must never sit indefinitely with an ambiguous NULL
`next_fire_at`.

**At create/update**, a cron expression must be syntactically valid **and** must produce a valid next
occurrence under its timezone. If it cannot produce one within the evaluator's supported horizon, the
create/update is **rejected** with a safe validation error.

**If an already-stored timezone later becomes unresolvable** (the tz database removes a name, or the
timezone data is downgraded):

1. the due occurrence is recorded safely as `skipped` with a static code;
2. the trigger is automatically made **inactive** — `enabled = false`, `next_fire_at = NULL`;
3. the user must correct its timezone before re-enabling.

It must **not** be silently reinterpreted in UTC, and it must **not** spin on every scheduler tick.

This is what allows the database to enforce the shape below without ambiguity.

### The next_fire_at invariant

```
CHECK (
    kind NOT IN ('one_time','interval','cron')
    OR (enabled = 0 AND next_fire_at IS NULL)
    OR (enabled = 1 AND next_fire_at IS NOT NULL)
)
```

`next_fire_at` is **durable scheduling state**, derived transactionally from the frozen schedule
configuration and the prior occurrence — not a cache. The invariant is expressible because both
terminal paths converge on "disabled": one-time completion, and fatal cron evaluation failure. A stale
`next_fire_at` on a disabled trigger, or a missing one on an enabled trigger, is therefore a constraint
violation rather than a state a later reader has to reason about.

### Misfire and downtime — `COALESCE_ONE`

Restart storms are the second-largest risk in Stage E, after amplification. The policy is a **closed
enum with exactly one member, fixed in Stage E**:

```
MisfirePolicy:
    COALESCE_ONE
```

| Kind | Behaviour on resume |
|---|---|
| `one_time` | a due-but-unmaterialized instant fires **once** |
| `interval` | **at most one** catch-up occurrence, at the latest missed nominal instant; then `next_fire_at` advances to the first strictly-future nominal time. Three days down with a five-minute interval produces **one** Run, not 864. |
| `cron` | identical |
| `webhook` / `event` | not applicable — a missed delivery is the sender's problem; a missed event was simply never published |

It is stored as a column with a one-value `CHECK`, not a boolean, so a second policy later is a
migration that widens one `IN` list — no data migration, no reshape. The same reasoning `0005` used to
widen `runs.status_value`.

**Unbounded historical backfill is refused.** There is no "replay all missed occurrences" mode, no
per-trigger backlog setting, and no configuration under which a misconfigured interval produces a Run
storm on restart.

### UTC storage, and one timezone used only for evaluation

Every durable instant in Stage E is an **absolute UTC `datetime`**, through the repository's existing
`UTCDateTime` type — the same column type every Stage B–D timestamp uses, and the same thing the
`Clock` protocol and `require_utc` already enforce.

An IANA timezone name is **configuration input used only at evaluation time**. It is never the storage
form of an instant. A `timezone` column exists on `cron` triggers alone.

### The DST contract, stated so it can be tested

Not "the library handles DST". The frozen behaviours:

| Case | Behaviour | Grounds |
|---|---|---|
| **Spring forward** — a local time that does not exist | The occurrence is materialized at the moment the local clock reaches the target: the skipped interval fires immediately when the gap ends. `nominal_at` is the resolved UTC instant, so identity stays unique and single. | Matches Debian cron, so the behaviour a self-hosting operator already expects is the one shipped. |
| **Fall back** — a local time that occurs twice | Fires **once**, on the first (pre-transition) occurrence. The second pass is suppressed, not materialized as a second occurrence. | Prevents a silent 2× amplification every autumn. |
| **A schedule coarser than the shift** | The nominal instant is computed in the trigger's zone; the shift moves the UTC instant, not the local intent. | Local intent is what the user configured. |
| **User changes the timezone** | A schedule-defining edit: `config_revision` increments, `next_fire_at` recomputes forward. Existing occurrences keep their stored `nominal_at` and `trigger_revision`. | Edit semantics below. |
| **Timezone removed / unresolvable at fire time** | skip the occurrence, then disable the trigger as described above. | A visible skip beats a silent reinterpretation. |

### Edit and revision semantics — stated truthfully

`config_revision` on the definition and `trigger_revision` on the occurrence exist to answer exactly
one question:

> **"Was this occurrence materialized while the definition was at revision N?"**

They prove **that** and nothing more. In particular:

- `trigger_revision` does **not** by itself preserve the full historical definition contents. The old
  cron expression, timezone, input text and Agent target **cannot** be reconstructed from the revision
  number alone.
- The historical execution truth for a materialized occurrence is the **immutable occurrence fields**
  (`nominal_at`, `owner_user_id`, `agent_instance_id`, the linked `run_id`) plus **the created Run's own
  immutable snapshot** — which already carries the Agent definition identity, provider, model, all
  effective limits and the grant cutoff.
- A **full `TriggerRevision` history table is out of Stage E.** It is not built, and no claim is made
  that depends on it.

**The rule this exists to enforce, and the one an implementation must not break:** occurrence and UI
history must **never display the current trigger configuration as though it were the historical
configuration that produced an older occurrence.**

Which edits belong where:

| Field | Editable | Bumps `config_revision` |
|---|---|---|
| `agent_instance_id` | yes | yes |
| `input_text` | yes | yes |
| `run_at` / `interval_seconds` / `cron_expression` / `timezone` / `event_type` | yes (kind-dependent) | yes |
| `display_name` | yes | **no** — the revision means "the configuration that determines what fires", not "the row changed" |
| `kind` | **no** — delete and recreate | — |
| `owner_user_id`, `id` | **no** | — |

Already-created occurrences and Runs are **never mutated** by an edit. `next_fire_at` is recomputed
forward from `now` under the new configuration, never left pointing at an instant the old
configuration chose. Because occurrence identity for schedules is `(trigger, nominal_at)`, a cadence
change simply produces different future instants — no old identity is reused and no historical row is
invalidated.

An edit racing a fire resolves on the same row inside one transaction (below); the occurrence records
the revision it actually observed.

### Delete and archive semantics

| Operation | Behaviour |
|---|---|
| Disable | the ordinary lifecycle action, always available |
| Delete | allowed **only when the trigger has zero occurrences and zero associated Runs**. Otherwise refused with a conflict. |
| Historical occurrences after delete | cannot exist under that precondition, so **no path destroys occurrence evidence** |
| Archive / soft delete | **not introduced.** No table in this schema carries a `deleted_at`, and adding one for a single resource family would be a new pattern rather than a followed one. |

This follows the MCP-connection precedent: `DELETE /api/v1/mcp-connections/{id}` exists and refuses
when the connection has history. Stage E mirrors the shape — a delete route that exists, that is honest
about what it refuses, and that never destroys evidence.

Every new foreign key is `ON DELETE RESTRICT`, matching all thirteen existing foreign keys in this
schema. An Agent Instance with triggers therefore cannot be deleted, consistent with the fact that no
Agent Instance delete route exists at all.

### Agent disabled at fire time

The triggered path inherits the manual path's authority **exactly**, because it calls the same
function rather than re-implementing the check.

- The durable submission re-checks owner and `enabled` **inside its own transaction** and refuses a
  disabled or foreign Agent Instance.
- On refusal, **no Run is created.** The occurrence is written `skipped` with a static code, in the same
  transaction — so the refusal is durable and visible rather than an error in a log.
- **A trigger never creates a Run that a manual submission would have refused.**
- **The trigger is not silently disabled.** For `interval` and `cron`, it stays enabled and its next
  occurrence is attempted on its own terms, producing a run of `skipped` occurrences followed by a
  normal `run_created` once the Agent is re-enabled — a history that explains itself. For `one_time`,
  the occurrence consumes the instant and the trigger becomes terminal (above).
- One Agent being disabled never aborts a tick for other triggers.

### Scheduler process topology — a dedicated process

```
apps/scheduler   composition root, config, loop, lifecycle
```

Its **only** job is determining due occurrences and submitting ordinary Runs. It must not execute Runs,
must hold **no provider credential**, and must expose **no HTTP surface**.

**Why a separate process rather than a loop inside the Worker:** the repository's own guards prove that
a structural module split — not reviewer vigilance — is what keeps execution-plane writes unreachable
(the control plane cannot claim, start or terminalize; the Worker exposes no HTTP surface). A scheduler
module that never imports the execution persistence inherits that property. A loop inside the Worker
would sit in the one module that legitimately *may* claim, and would additionally couple automation
uptime to execution-process uptime: every Worker restart would interrupt scheduling, which contradicts
the stage's outcome.

The process mirrors `apps/worker` exactly: its own `config.py`, `app.py` composition, `main.py`
lifecycle, a `NERVOS_*_READY_FILE` test seam, a schema-revision guard, and a `close_*` teardown. It
opens no remote connection, so it has no transport-session lifecycle to drain.

### SchedulerService shape

A provider-neutral application service. The outer process owns clock, sleep, startup and shutdown.

```
SchedulerService.tick(now: datetime) -> SchedulerTick
SchedulerService.startup_pass(now: datetime) -> SchedulerTick
```

One iteration: discover a **bounded** set of due, enabled schedule triggers in deterministic order; for
each, in one transaction, re-read the definition, re-check authority, insert the occurrence, submit the
ordinary Run, advance `next_fire_at`; return a structured result (examined / materialized / skipped /
duplicated counts and the occurrence ids) so tests assert outcomes rather than side effects.

Because `now` is a parameter, **no test waits for a minute.** This mirrors `ReclaimLoop`'s
`sweep_once` / `startup_pass` shape and its injected clock.

### Fixed polling, and why there is no idle backoff

```
SCHEDULER_POLL_INTERVAL_SECONDS = 5.0     fixed
```

**There is no escalating idle sleep in Stage E.** A backoff to a longer idle interval would mean a
newly-created schedule could wait that long before its first check, contradicting the sub-poll-interval
scheduling bound the service claims. Since there is no inter-process wake mechanism in Stage E,
introducing an idle backoff without one would trade a real latency guarantee for a small amount of idle
database work. The poll is therefore fixed and capped at five seconds.

A dedicated wake mechanism is a future decision, not part of this freeze.

### Multi-instance behaviour — the database arbitrates, not the process

**There is no scheduler lease, no leader election, no advisory lock, and no lock table.**

- Two scheduler instances ticking simultaneously serialize on the SQLite write lock. One wins the
  insert; the other's conditional insert affects zero rows and it moves on.
- A `PersistenceContention` after the retry budget is surfaced as a **deferred tick**, never as an
  error — the same way a Worker treats a deferred claim.
- The repository's one existing lease (`jobs.lease_expires_at`) exists because *execution* is long — it
  spans a provider call — whereas materialization is short and touches only local rows.
- There is no long-lived "in progress" occurrence for a second instance to steal, duplicate or
  reclaim, so a lease would protect nothing.

**No transaction is held while sleeping and none spans network I/O.**

### Crash atomicity

The whole materialization — occurrence insert, Run + Job insert, occurrence-to-Run link, and schedule
advancement — is **one short transaction**.

| Crash point | Result | Repair |
|---|---|---|
| before the transaction begins | nothing happened; trigger still due | none — next tick |
| after the due scan, before the write lock | same | none |
| after `BEGIN IMMEDIATE`, before the occurrence insert | rolled back; still due | none |
| **after the occurrence insert, before the Run insert** | **rolled back; still due** | **none** |
| **after the Run insert, before the Job insert** | **rolled back; still due** | **none** |
| **after the Job insert, before the schedule advance** | **rolled back; still due** | **none** |
| after commit | occurrence, Run, Job and the advanced schedule are all durable | **C3/C4 owns the Run from here** |
| after commit, before the process records anything in memory | the process's view is stale; the database is correct | none — nothing durable depends on process memory |

The middle three cases are exactly what a two-transaction design cannot handle without a repair worker.
They are rollback-safe because they are one transaction, and that is the whole reason the architecture
insists on it.

**One constraint this rests on:** the transaction contains **no network I/O and no sleep**. The
webhook body has already arrived and the event envelope is already in memory before the transaction
opens; nothing inside it can block on a remote party.

### Bounded due scanning

The scheduler never loads every trigger. The scan is indexed, bounded and deterministically ordered:

```
WHERE next_fire_at IS NOT NULL AND next_fire_at <= :now
ORDER BY next_fire_at ASC, id ASC
LIMIT :batch
```

supported by a **partial index** on `(next_fire_at, id) WHERE next_fire_at IS NOT NULL`, so webhook and
event triggers never appear in it. The ordering gives stable keyset order under equal instants,
matching how the Worker claims Jobs and how C3 reclaims expired claims.

`SCHEDULER_DUE_BATCH = 32` bounds one tick's work.

### Stage E creates no second capacity system

When many schedules fire, they create **ordinary Runs and Jobs**. Stage C's existing queue,
concurrency, fairness and admission backpressure control actual execution.

Stage E bounds only **how many due occurrences it materializes per tick**. It never refuses a valid
trigger because Worker concurrency happens to be full — queueing is Stage C's job, and C6's existing
admission limits already answer capacity.

### The scheduler schema guard

The Scheduler **must** use the same explicit schema-revision guard as the Worker, and must refuse to
operate against the wrong migration head. The guard is promoted into a shared provider-neutral location
so both processes call one implementation, and the Worker's behaviour is unchanged apart from the
import location.

**The API gains no schema guard in Stage E.** It has none today, adding one is a production behaviour
change with its own blast radius, and no API schema-refusal capability is claimed.

### Bounds and constants

Declared as module-level internal constants, not operator settings — following the repository's own
rule that an exposed setting needs a demonstrated operator requirement.

| Constant | Value |
|---|---|
| `SCHEDULER_DUE_BATCH` | 32 |
| `SCHEDULER_POLL_INTERVAL_SECONDS` | 5.0 (fixed, no idle backoff) |
| `INTERVAL_MIN_SECONDS` | 60 |
| `INTERVAL_MAX_SECONDS` | 31_536_000 |
| `MAX_CRON_EXPRESSION_LENGTH` | 128 |
| `MAX_TIMEZONE_NAME_LENGTH` | 64 |
| `OCCURRENCE_PAGE_LIMIT_MAX` | 200 |
| `TRIGGER_PAGE_LIMIT_MAX` | 50 |
| `MAX_TRIGGER_INPUT_CODE_POINTS` | 4000 (reuses `RunLimits.input_max_code_points`) |
| `MAX_TRIGGER_INPUT_BYTES` | 8000 (reuses `RunLimits.input_max_bytes`) |
| `MISFIRE_POLICY` | `COALESCE_ONE` |

### Trigger input contract

A trigger supplies the Run's existing `input_text`, and nothing else. No second prompt architecture.

- The static trigger text is required, non-blank, and bounded to the existing Run input maximum.
- The **system instruction is never composed from trigger text or from payload.** It remains the frozen
  trusted-chat constant. There is no code path in which external text could reach it.
- External event and webhook material is appended **as delimited data** after the operator's text, in
  canonical form.
- The composed result must satisfy the target Run's own input bounds. If it cannot, the occurrence is
  `skipped` and **no Run is created** — the system refuses rather than truncating, because truncation
  could cut the operator's instruction in half while keeping the payload.

### Observability ownership

| Record | Authority |
|---|---|
| TriggerDefinition | configuration |
| TriggerOccurrence | trigger-materialization history |
| Run / Job / Attempt | execution |
| ToolInvocation | tool execution |
| RunEvent | safe Run chronology |

No one of these is allowed to become a second account of another's facts.

### No new RunEventType

**Stage E adds no Run event type.** Scheduling audit lives in `trigger_occurrences`, and the Run
timeline is untouched.

The vocabulary is a frozen schema fact: `run_events.event_type` carries a `CHECK` enumerating every
legal value, and SQLite cannot alter a `CHECK` — every constraint change in this repository is a full
table rebuild of an append-only table whose contiguous-sequence invariant the public timeline depends
on. Beyond the cost, the candidate values (`run.triggered`, `schedule.fired`, `webhook.received`) are
pure duplicates of what `trigger_occurrences` already records, and a second record of one fact is
precisely what this ADR forbids.

### No automatic Run → Event recursion

Stage E **never** converts a Run Event into a trigger input. If every Run Event were republished as an
internal event, `trigger → Run → run.succeeded → trigger → …` would be an accidental automation loop
with no operator intent, no depth bound and no visible cause.

The `nervos.*` event namespace is **reserved and empty**: no system event is emitted in Stage E. A
future stage that wants an agent to trigger another agent must do it through an **explicit** event
publication, not an implicit bridge.

### Trigger management API

Owner-scoped, authenticated, Origin-protected, under the existing `/api/v1` prefix, following the
repository's route conventions. Occurrence history uses **keyset pagination** with a bounded page size,
matching the Run and Event read surfaces. No Job id, Attempt id, claim token, lease, worker identity,
scheduler internals, payload or secret digest is ever exposed.

A webhook's secret is returned **once**, at creation or rotation, and is never retrievable afterwards.

### Minimal frontend surface

Stage E includes a minimal Automations surface — list, create (one-time / interval / cron), edit,
enable/disable, next fire time, last occurrence, occurrence history linked to Runs, and webhook
creation with a one-time secret display and rotation.

It is small on purpose. No visual workflow builder, no calendar product, no DAG editor, no event-DSL
editor. It uses the existing polling conventions; **no SSE and no WebSocket**.

Without it, Stage E's headline capability would be reachable only by `curl`, which would make a
backend milestone masquerade as a user-facing one.

### No run-now

There is no "run now" action on a trigger, because it would have to fabricate an occurrence that never
was due — muddying the one record that makes occurrence history truthful. Manual Run remains manual
Run.

### Cancellation is not trigger disable

Disabling or deleting a trigger **does not cancel Runs already created**. Stopping an existing Run is
C5 cancellation, and only C5. There is no trigger-level retroactive mutation of execution.

### Milestones

| Milestone | Scope |
|---|---|
| **E0** | this architecture freeze — documentation only, no runtime code |
| **E1** | durable trigger/occurrence domain, migration `0008`, the shared acceptance seam and atomic materialization foundation |
| **E2** | scheduler: one-time / interval / cron, timezone, misfire, multi-instance, restart, schema guard |
| **E3** | webhook ingress, secret authentication and rotation, idempotency |
| **E4** | internal events, management API, occurrence history, minimal frontend |
| **E5** | integrated acceptance and Stage-E closeout |

### Dependency decisions

| Package | Decision |
|---|---|
| a small cron parser/evaluator | approved for Stage E; **installed at E2**, when evaluation is actually introduced. Not installed in E1 merely because it is approved. The chosen library is a parser/evaluator only — never a scheduler — and it is importable only in the infrastructure adapter. |
| `tzdata` | approved; installed at the milestone that genuinely needs cross-platform IANA resolution. `zoneinfo` requires a timezone database that Linux distributions and CI runners supply from the OS but Windows and slim containers do not, so the frozen UTC/timezone contract would otherwise behave differently by platform. |

Architecture approval is independent of installation timing.

## Consequences

**Stage E acquires no execution authority at all.** It creates Runs and nothing else. Every property C
and D built — claim fencing, retry safety, cancellation authority, concurrency limits, grant cutoffs,
no-replay-after-dispatch — applies to a triggered Run without Stage E writing a line of it, because a
triggered Run *is* an ordinary Run.

**The cost is that scheduling is genuinely separate machinery.** A new process, a new domain, two new
tables and a new transactional seam have to be built and tested. The benefit is that a scheduler bug
can create a Run at the wrong time, and cannot execute one at all.

**Some configuration is refused that a more permissive product would accept.** Interval scheduling has
no timezone; cron has no seconds field and no `L`/`LW`/`#`; a one-time trigger cannot be re-enabled
after it fires; a completed schedule cannot be rewound. Each refusal removes a class of ambiguity that
would otherwise have to be documented rather than enforced.

**Bounded misfire means a long outage produces one catch-up Run per trigger, not a backlog.** That is
the intended behaviour, and it is stated rather than discovered.

**History grows without bound.** No retention or cleanup subsystem is built, and that is recorded as an
operational limitation rather than solved speculatively.

**`trigger_revision` proves less than it looks like it proves.** It records which revision was in force
when an occurrence materialized; it does not preserve that revision's contents. The historical
execution truth is the immutable occurrence fields plus the created Run's own snapshot. Any UI that
renders current configuration in place of historical configuration is a defect, not a display choice.

**Unmatched internal events leave no durable trace.** An operator debugging "my event fired nothing"
has logs, not a row. Accepted deliberately: the alternative is an event log that becomes a second
account of what happened, and a table that exists only so it can be replayed is a table that will
eventually be replayed.
