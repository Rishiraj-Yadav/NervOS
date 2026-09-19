# ADR 0017 — Tool-Loop Durability: Ambiguity Without Replay

Status: Accepted for Stage D implementation

This ADR freezes what happens to a Run when a model call and an external action are interleaved —
across a crash, a retry, a cancellation, a timeout, and a fleet of Workers. It is the part of Stage D
where a mistake cannot be undone by fixing it later, because the failure mode is an external system
being asked to do something twice.

Authority and audit are ADR 0015; the protocol and connection boundary are ADR 0016.

## Context

Stage C's replay safety rests on one property: **exactly one outcome code is positively safe to
replay.** `model_rate_limited` proves the provider declined the request, so re-running the Attempt
from its immutable input reproduces the identical starting state. Every other code is either terminal
or `AMBIGUOUS`, and `disposition_for` fails closed to `AMBIGUOUS` for anything unrecognized.

That reasoning holds because in Stage C an Attempt's only external effect *is* a model call. Stage D
breaks the premise: an Attempt can now cause **durable, remote, irreversible side effects** through
tools, and the existing whole-Attempt retry would re-run the handler **from the immutable Run input**
— re-issuing any tool the first Attempt already performed.

The dangerous sequence is short and ordinary:

```
model turn 1  →  tool create_issue  →  SUCCEEDS remotely and durably
model turn 2  →  model_rate_limited
```

Under unmodified C4 this schedules `retry_wait`, a fresh Attempt is claimed, and `create_issue` runs
again. Everything else in Stage D is downstream of solving this.

A second hazard came from the opposite direction. C3 reclaims a lost Worker's work by classifying the
Attempt as pre-start (safe to re-queue) or post-start (ambiguous, never replayed). Tool loops change
what "started" means, and a formulation that treats a crash before a tool's dispatch as *provably
safe* would license exactly the replay this ADR forbids.

## Decision

### NervOS owns the loop

No provider SDK auto-execution is enabled. The loop lives above `ModelCompletion`, inside the
execution boundary Stage C already established, and is reachable only through a **new agent
definition version** — `nervos.chat@1`'s behaviour is not modified.

```
assemble(request): system instruction + conversation + GRANT-FILTERED catalog
   │
   ├─ model call (existing provider timeout)
   │
   ├─ final text? ─────────────────────────► done
   │
   └─ tool calls? ─► for each, SEQUENTIALLY, in the order the model returned them:
                       1. resolve against the Run's catalog   (unknown ⇒ deny, audit)
                       2. validate arguments vs the canonical schema (invalid ⇒ deny, audit)
                       3. permission check, LIVE                        (ADR 0015)
                       4. commit `requested` + the decision
                       5. commit `started`   ← THE AMBIGUITY BOUNDARY
                       6. dispatch
                       7. normalize the result
                       8. commit the terminal state (fenced)
                       9. append the observation
   └─ repeat

terminates on: final text · cancellation · failure · timeout · a loop limit
```

**Multiple calls in one turn are sequential, in the model's order.** Parallel execution would need
per-invocation fencing, out-of-order sequencing, and partial-failure semantics that multiply the
ambiguity surface for a latency win nothing requires. Sequential also gives a property the contract
depends on: an authority check runs **between** calls, so a revocation or cancellation observed after
call 1 is honoured before call 2 starts.

**No database transaction spans a model call, an MCP request, a stdio execution, or any external
I/O.** Every step above is either a short transaction or an external call, never both.

### The provider carrier contract (verified at D0)

Both provider contracts were verified against current official documentation rather than assumed,
because the loop's termination condition depends on them.

**Anthropic** (verified 2026-09-17, `platform.claude.com/docs/en/agents-and-tools/tool-use/`):

- tool `name` — *"Must match the regex `^[a-zA-Z0-9_-]{1,128}$`"*; definition is
  `{name, description, input_schema}` with optional `strict`, `input_examples`, `cache_control`;
- a tool call is a `tool_use` content block `{type, id, name, input}`;
- `stop_reason: "tool_use"` marks a turn that wants tools;
- a result returns as a `tool_result` block carrying the original `tool_use_id`;
- **parallel tool use is on by default** and is disabled with
  `tool_choice: {type: "auto", disable_parallel_tool_use: true}`;
- `tool_choice` accepts `auto` (default when tools are present), `any`, `tool`, `none`.

**OpenAI**: function `name` — *"a-z, A-Z, 0-9, or contain underscores and dashes, with a maximum
length of 64"*; `{type:"function", name, description, parameters, strict}`; a call is a
`function_call` item with a `call_id`, and the result returns as `function_call_output` with the same
`call_id`; `tool_choice` accepts `auto`, `required`, `none`, and specific-tool forms.

**Two consequences the loop must honour, neither of which the first design captured.**

First, **the cross-provider name bound is 64**, not 128 — the stricter of the two — which is what
ADR 0015's naming algorithm reserves.

Second, and load-bearing: **an Anthropic assistant turn may contain text *and* `tool_use` blocks
together.** Its documentation is explicit that a model "often comments on what it's doing … before
calling tools", so a turn is not either-or. The loop therefore discriminates on **whether the turn
carries tool calls**, never on whether it carries text. `ModelResponse.text` remains a non-optional
field that may legitimately be empty, and only the **final** turn — the one with no tool calls — must
carry non-blank text. A loop that keyed off "text is empty" would treat a thinking-out-loud tool turn
as a completed answer and silently drop the work.

Stage D additionally sets `disable_parallel_tool_use: true` where the provider supports it, so the
provider's model of the turn matches our sequential execution. It is a hint, not a guarantee, and a
provider with no such switch may still return several calls — which is why the sequential path exists
regardless.

**No provider-specific object crosses into `nervos-core`.** The loop sees only canonical carriers — a
tool name, a call id, an argument blob, a result — and never an Anthropic content block or an OpenAI
output item. Each adapter translates in both directions and is the only place that knows its own wire
shape. The existing guard that keeps provider names out of the shared execution path extends to cover
these carriers, so a future provider is added by writing one adapter and no loop change.

### Steps 4 and 5 are separate commits, and 5 precedes the network

`requested` commits the intent, the permission decision, and the definition fingerprint before any
external effect, so "did we intend this?" is durable. `started` commits **immediately before
dispatch**, and that commit is the boundary at which the outcome becomes unknowable.

This is not a new mechanism. `start_attempt` already documents exactly this pattern for the provider
call: *"Commit the execution-start boundary before any external call is made … a caller may therefore
invoke the provider exactly when True is returned, and must not invoke it otherwise."* Stage D applies
it one level down.

### The invocation state machine

```
              ┌──────────────► denied        (terminal, no dispatch)
              │
 requested ───┼──────────────► cancelled     (terminal, no dispatch)
              │
              └── started ────┬─► succeeded   (terminal)
                              ├─► failed      (terminal, known safe failure)
                              └─► ambiguous   (terminal, dispatched, outcome unknown)
```

`denied` and `cancelled` are kept apart because they are different audit facts — refused by policy
versus stopped by the owner — even though neither dispatched.

**The dispatch predicate**, used by the replay guard:

```
dispatched  ≡  status IN ('started','succeeded','failed','ambiguous')
```

`requested`, `denied` and `cancelled` are deliberately **not** dispatched, so an Attempt that intended
a call but provably never made one stays safely replayable by C4.

### Every tool-loop crash is post-start, and is never replayed

A first formulation carried two rows saying a crash before dispatch was "replayable if C4 allows".
That is impossible, and the error is worth recording because it is easy to make twice.

**A process crash cannot call `record_failure`.** C4's retry is reachable only when a *live* Worker
explicitly settles a `model_rate_limited` failure inside a fenced transaction. A crash is handled by
C3's reclamation, which keys **only** on `execution_started_at` and job/attempt status. And because
**every tool invocation is created inside a started Attempt**, every crash point inside the loop is
post-start — so C3 routes all of them to the post-start ambiguous path, which marks the Run `failed`
and **never re-queues it**.

| # | Crash point | Durable truth | Replay? | Run outcome |
|---|---|---|---|---|
| A1 | Before `requested` commits | nothing about this tool | **never** | ambiguous → `run.failed` |
| A2 | After `requested`, before `started` | intent for **this** tool; no dispatch for it | **never** | ambiguous → `run.failed` |
| A3 | After `started` commits, before dispatch | dispatched marker committed | **never** | ambiguous → `run.failed` |
| A4 | After dispatch, before response | remote may have acted | **never** | ambiguous → `run.failed` |
| A5 | After response, before result commits | remote **did** act | **never** | ambiguous → `run.failed` |
| A6 | After result commits, before the next model turn | invocation terminal | **never** | ambiguous → `run.failed` |
| A7 | Mid-turn, between two sequential calls | earlier calls terminal; in-flight one per A1–A6 | **never** | ambiguous → `run.failed` |
| A8 | During an in-Attempt model backoff | prior invocations terminal; no new dispatch | **never** | ambiguous → `run.failed` |
| A9 | After final text, before finalization | invocations terminal; no Run terminal | **never** | ambiguous → `run.failed` |

**A2 needs a qualifier that the first formulation omitted, which is why A7 exists.** "This tool never
dispatched" is true at *per-tool* granularity and false at *per-Attempt* granularity — and the replay
guard evaluates per Attempt. If an earlier call in the same turn already dispatched, then at Attempt
granularity something **did** leave the process, so A2's verdict is "never", identical to A3–A6.

**A3 looks pedantic and is not.** The `started` commit precedes dispatch, so a crash in that window
means the call almost certainly never went out — but "almost certainly" is not a durable fact, and
treating it as one is precisely the reasoning that produces double-sends.

**A6 is the case where the tool genuinely succeeded and the Worker died before the next turn.** The
Run is ambiguous **because the agent did not finish**, not because the tool's fate is unknown. Both
levels are recorded truthfully and they do not contradict.

### C4: the two-layer design

**Layer 1 — bounded in-Attempt model retry.** Inside the loop, a rate-limited *model turn* is retried
**in place**, up to a bounded count, reusing C4's existing delay ladder. This is safe because it
re-issues a **model request**, which has no external side effect, and it never re-enters a tool that
already ran: the loop continues from its in-memory conversation state rather than restarting. The
60-second renewable lease comfortably covers the maximum backoff.

**Layer 2 — the dispatch predicate narrows C4's existing whole-Attempt retry**, inside the same
fenced transaction that would schedule it:

```
may_schedule_durable_retry  ≡  disposition == SAFE_TO_RETRY
                           AND  error_code == MODEL_RATE_LIMITED
                           AND  attempt_count < max_attempts
                           AND  NOT EXISTS (
                                    SELECT 1 FROM tool_invocations
                                    WHERE attempt_id = :attempt
                                      AND status IN ('started','succeeded','failed','ambiguous'))
```

If that fails, the Attempt is terminalized instead of scheduled.

**Why it is sound.** An Attempt with no dispatched invocation has produced exactly one external
effect — a model call that provably failed — so re-running it from the immutable input reproduces the
identical starting state. That is C4's existing guarantee, unchanged. An Attempt with a dispatched
invocation can never be replayed by any path, because the guard is evaluated inside the same
transaction that schedules the retry, from durable rows only. **The change narrows C4's replay
surface; it cannot widen it.**

**Three tests are required**, and the first is the reason this ADR exists:

1. **Invariant.** **No ToolInvocation may reach `requested` or `started` before `start_attempt` has
   successfully committed `execution_started_at`.** This is the premise the whole crash matrix rests
   on — it is what makes "every tool-loop crash is post-start" true — and it is currently an
   *asserted* property rather than an enforced one, so it must be pinned by a test rather than by
   reading the code.
2. Tool succeeds → next model turn rate-limits → in-Attempt retries exhaust → no durable
   whole-Attempt retry is scheduled → Run fails `model_rate_limited` → **the tool ledger shows exactly
   one call**.
3. No tool dispatched → `model_rate_limited` → **the existing durable C4 retry still fires** and the
   Run succeeds on its second Attempt.

Test 2 is the adversarial case §72 names as a plan blocker if unresolved; test 3 is its control,
proving the narrowing did not simply disable C4 retry altogether.

**One precision note, so the two from-scratch restart paths are not confused.** C4's pre-dispatch
retry is the only path that restarts *a loop* from scratch. C3's **pre-start re-queue** is also a
from-scratch restart, but it exists only **before** `start_attempt` commits — at which point no loop
and no conversation ever existed — so it needs no conversation state either. Reading the rule as if
C4 were the sole restart path is a harmless over-simplification today, but it is worth naming both so
that a future change to either path is reviewed against this section.

**The cost, stated honestly:** a tool-using Run that hits a sustained rate limit after doing real
work **fails**, where a pure-chat Run would have retried. That is the correct trade — safety over
automatic continuation.

### C3: post-start is never reconstructed

`execution_started_at` remains the **only** execution boundary; Stage D adds no second notion of
"began". A Worker lost before `start_attempt` is pre-start and C3 re-queues it under the existing
recovery budget — and no tool could have run, because tools only run inside a started Attempt. A
Worker lost after `start_attempt` is **ambiguous and never reconstructed**, regardless of whether any
tool ran.

That is *more* correct in Stage D, not less: a post-start Attempt may hold in-memory conversation
state that is not durable, so reconstructing it would require replaying exactly the tool calls this
ADR forbids.

**Therefore Stage D persists no conversation state and has no resume-after-tool.** That is not a gap;
it is the consequence of the no-replay rule, and it is what keeps Stage F out of Stage D.

### C5: cancellation

| Cancellation observed… | Behaviour |
|---|---|
| before a tool is dispatched | invocation closed **`cancelled`**; the tool never executes; zero external calls |
| while the model call is in flight | existing C5 behaviour unchanged: local authority revoked, Run not resurrectable, late model result discarded |
| **after** a tool was dispatched | in-flight invocation closed **`ambiguous`**; no result — even a later success — may resurrect the Run; the remote action may still have occurred and the audit says so |
| while the next turn is prepared | the loop checks authority before dispatching; the tool never starts |

Completion is fenced on the same Attempt authority every other Stage C write uses, so **a late tool
result cannot overwrite truth**. Cancellation neither invents nor hides a tool event: a cancelled
Run's audit shows exactly what happened.

### Tool timeouts

One deadline per call, from an immutable Run limit, started when dispatch begins — distinct from the
model provider timeout, which is unchanged. The Worker's lease is **not** a task timeout; it is a
renewable liveness window, so a long tool call is safe by construction.

**The relationship between the tool deadline and the lease is stated explicitly, because it is easy to
get wrong.** A tool call may legitimately outlive one 60-second lease window, and the maximum tool
deadline exceeds it. That is safe **only because lease renewal runs as an independent task** — the
same property that already lets a long provider call keep its own claim. It follows that **tool
execution must never block the event loop**: a synchronous or blocking call inside the loop would
starve renewal, the lease would lapse, and the Attempt would be reclaimed as post-start ambiguous.
That outcome is *safe* — no replay, no side effect repeated — but it is a wasted Run, so every tool
dispatch is awaited asynchronously and any inherently blocking work is offloaded, exactly as the
Worker already treats blocking persistence.

**A timeout after dispatch is `ambiguous` and is never retried.** stdio teardown is terminate-then-kill
with a bounded grace and no unbounded await.

### Failure classification

| Class | Dispatched? | Model sees | Run | Retryable |
|---|---|---|---|---|
| Unknown tool · permission denied · schema invalid · unsupported schema | no | safe error observation | continues | n/a |
| Source unavailable (before dispatch) | no | safe error observation | continues | yes, in-Attempt, bounded |
| Server rejected · tool returned failure (`isError`) | **yes** | safe error observation | continues | **no** |
| Timed out · transport lost · malformed result · internal failure | **yes** | — | **fails** | **never** |
| Unsupported output type | **yes** | safe "unsupported output" observation | continues | no |

**The dividing line is dispatch.** Before it, a failure is local, provable, and safe to observe. At or
after it, the outcome may already be real elsewhere. **No code may infer replay safety by
string-matching an exception.**

**Ambiguity is terminal for the Run.** Returning an "outcome unknown" observation would invite the
model to try again — the exact thing this ADR forbids. Consecutive *safe* failures are capped so a
model calling only broken tools is stopped rather than allowed to spin.

### The no-replay rule, stated exactly

**The NervOS runtime never automatically replays a dispatched external tool call** — not because a
response was lost, not because a timeout occurred, not because a Worker failed, not because a Run was
retried, and not because a transport disconnected.

**A later model turn may independently request the same granted tool again.** That is a **new
ToolInvocation**: a new internal id, a new `tool_sequence`, a fresh live permission check, and a fresh
audit record. It is **not** "runtime replay", and it must not be described as one.

Because the user granted the capability, repeated deliberate model requests remain a **residual
agent-behaviour risk**. Stage D does **not** claim the system can always distinguish a semantically
duplicated intent from a legitimately repeated one, and no documentation or UI copy may imply that it
can. `idempotentHint` is untrusted and cannot license a replay.

### Loop limits

Model calls and tool calls are bounded per Run; tool timeout and result size are bounded; consecutive
tool failures are bounded by a frozen constant. All are **immutable Run limits**, snapshotted at
submission so a policy change cannot alter an in-flight Run's contract, and **the model never chooses
any of them**.

### Output handling and the injection boundary

Supported output is **text plus bounded structured content**. Images, audio and embedded resources
produce an `unsupported_output` observation naming the type — **never a stringified or base64 blob
pushed into the model's context**. Truncation is recorded durably and disclosed to the model; the
audit never claims a complete result that was cut.

**Tool output is untrusted external content**, and the MCP specification offers no guidance on this —
the security burden is entirely NervOS's own. Observations are returned as a **distinct channel**
(`tool_result` / `role: tool`), never concatenated into the system instruction; descriptions are data
that can never alter policy; permission checks are repeated after every observation; result size is
bounded; and the catalog cannot change mid-Attempt.

**Residual risk, documented:** a model may be induced by a tool result into *requesting* a harmful
tool. The mitigation is not that the model resists — it is that **the request is still refused** when
no grant exists, and that a granted destructive tool's effect is bounded by what the user granted.

### C6: no second scheduler

A tool-using Run remains **exactly one active Job**, so the accepted concurrency caps, active/pending
predicates, and fairness rotation already cover the whole loop. Stage D introduces **no global tool
scheduler, no per-server call limiter, and no tool queue** — any of those would create a second source
of execution authority beside the Job lease, which the accepted architecture forbids.

### Run Events

Six event types are added to the public timeline — `tool.requested`, `tool.started`, `tool.succeeded`,
`tool.failed`, `tool.denied`, `tool.ambiguous` — plus a nullable `tool_invocation_id` linking each to
its durable record. **No `tool.cancelled` event is added**: C5's `run.cancelled` remains authoritative
for user cancellation, and a cancelled tool's own status is already visible on the invocation. The
public projection carries an allow-list of safe fields only — never arguments, results, digests, or
credentials.

## Consequences

A tool-using Run is **less** recoverable than a pure-chat Run: it fails where chat would retry, and a
crashed loop fails rather than resuming. That is the entire point. Remote side effects and billing may
occur in ambiguous crash, cancellation, and timeout cases, and no exactly-once claim is made anywhere.

Because no conversation state is durable, an interrupted multi-turn Run cannot be resumed by a future
milestone without revisiting this decision — deliberately, with the replay question answered first.
