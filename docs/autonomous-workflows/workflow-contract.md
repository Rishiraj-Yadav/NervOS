# Durable autonomous workflows — frozen implementation contract (W0)

Status: W0 contract freeze. Derived from `docs/adr/0039-durable-autonomous-workflows.md`
after inspecting the actual `Run`/`Job`/`Attempt` seams, the fenced Worker
finalization boundary, the Stage-F context/memory contract, and the Stage-H
approval/account/trust boundaries. Where ADR 0039 and the inspected code could be
read two ways, this document resolves it and says why.

Migration head when this contract was frozen: `0023_worker_sandbox_capability`.
This milestone adds `0024_durable_workflows`.

## 1. Entity separation

These are eight different things and none of them is a view of another:

| Entity | Row | Owns | Never |
|---|---|---|---|
| `AgentPackage` | `installed_package_versions` | installed artifact identity | workflow state |
| `AgentInstance` | `agent_instances` | an owner's configured agent | progress |
| `WorkflowExecution` | `workflow_executions` (new) | ordered steps + private application progress | conversation text, scoped memory |
| `WorkflowStep` | `workflow_steps` (new) | one step's pinned checkpoint + linked Run | execution authority |
| `Run` | `runs` | one bounded execution snapshot | workflow lifecycle |
| `Job`/`JobAttempt` | `jobs`, `job_attempts` | queue position and lease | application state |
| `Conversation` | `conversations` | chat turns | workflow state |
| `MemoryItem` | `memory_items` | owner/agent-scoped durable facts | workflow state |
| `WorkflowCheckpoint` | `workflow_checkpoints` (new) | one immutable revision of application state | instructions, permissions, memory |

**Waiting is workflow state, not a Run status.** `RunStatus` keeps exactly its five
existing members. A waiting workflow owns *no* Run, *no* Job and *no* Attempt, so
it consumes no Worker execution slot, no lease and no permanent package process.

## 2. Lifecycle transition table

`WorkflowStatus` is a new closed vocabulary. Terminal states are `succeeded`,
`failed`, `cancelled`, `needs_review`. `needs_review` is terminal for
*automatic progression* but is not "the workflow is over" — it is the explicit
"a human must look at this" state, and it retains all execution evidence.

| From | Event | To | Actor | Transaction |
|---|---|---|---|---|
| — | `create` | `pending` | owner (API) | workflow + step 1 + Run + Job + events, one `BEGIN IMMEDIATE` |
| `pending` | tick: due, unpaused, budget available | `runnable` → `running` | Scheduler | continuation Run + Job + step link + reservation, one transaction |
| `running` | fenced success, directive `next` | `running` | Worker | Run/Attempt/Job success + checkpoint N+1 + progress, one transaction |
| `running` | fenced success, directive `wait` | `waiting` | Worker | same transaction; sets `wait_kind` + wakeup/signal identity |
| `running` | fenced success, directive `complete` | `succeeded` | Worker | same transaction |
| `running` | Run terminal `failed`/`cancelled` | `needs_review` | Scheduler tick | projection only, evidence retained |
| `waiting` | time wakeup due | `runnable` | Scheduler tick | same transaction as the continuation insert |
| `waiting` | owner signal accepted | `runnable` | owner (API) | signal row + workflow advance, one transaction |
| `waiting` | owner decision accepted | `runnable` | owner (API) | decision row + signal row + advance, one transaction |
| any nonterminal | `pause` | same state, `paused = 1` | owner (API) | flag only; an in-flight Run still finishes |
| any nonterminal | `resume` | same state, `paused = 0` | owner (API) | flag only; **never** fabricates a signal |
| any nonterminal | `cancel` | `cancelled` | owner (API) | terminal; running Run cancelled separately through existing authority |
| any nonterminal | deadline passed | `failed` (`deadline_exceeded`) | Scheduler tick | terminal; no new dispatch |
| `running` | budget exhausted before accept | `failed` (`budget_exhausted`) | Scheduler tick | terminal |
| `needs_review` | owner archives | stays `needs_review` | owner (API) | retention/archival only |
| any | pinned identity changed | `needs_review` (`configuration_changed`) | Scheduler tick | never auto-repins |

Illegal transitions raise `WorkflowTransitionError`. Terminal states are
immutable except for archival metadata.

## 3. Typed interfaces

### 3.1 Domain (`nervos_core.domain.workflows`)

```python
class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNABLE = "runnable"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_REVIEW = "needs_review"


class WaitKind(StrEnum):
    TIME = "time"
    SIGNAL = "signal"
    OWNER_DECISION = "owner_decision"


class WorkflowDirectiveKind(StrEnum):
    COMPLETE = "complete"
    NEXT = "next"
    WAIT = "wait"


@dataclass(frozen=True, slots=True)
class WorkflowDirective:
    kind: WorkflowDirectiveKind
    wait_kind: WaitKind | None = None
    wakeup_at: datetime | None = None
    wait_seconds: int | None = None
    signal_key: str | None = None
    decision_key: str | None = None
    # Validation: COMPLETE -> every other field None.
    #          NEXT     -> every other field None.
    #          WAIT/TIME -> only wakeup_at.
    #          WAIT/SIGNAL / WAIT/OWNER_DECISION -> only signal_key / decision_key.


@dataclass(frozen=True, slots=True)
class WorkflowStepResult:
    """One bounded step outcome returned by a package, validated before terminalization."""

    directive: WorkflowDirective
    state: Mapping[str, JsonValue]  # the next checkpoint, bounds-checked
    summary: str | None  # <=512 chars, safe for the owner projection
    decision_request: WorkflowDecisionRequest | None
```

`WorkflowBudget` (frozen dataclass, ADR 0039 limits):

| Field | Default | Hard max | Meaning |
|---|---|---|---|
| `max_steps` | 32 | 128 | steps for the whole workflow |
| `model_call_reservation` | 256 | 2048 | **cumulative reserved**, not a per-step value |
| `tool_call_reservation` | 256 | 2048 | cumulative reserved |
| `output_token_reservation` | 262144 | 2097152 | cumulative reserved |
| `deadline_hours` | 24 | 168 (7 days) | forbids future dispatches |
| `max_retained_checkpoints` | 128 | 128 | terminal retention + owner archive |

Reserved is **not** used. Reservations are drawn down by accepted steps and are
never refunded, so a crash cannot buy free work by losing the counter.

### 3.2 Bounds that apply to every JSON payload

| Bound | Value | Applies to |
|---|---|---|
| `CHECKPOINT_MAX_BYTES` | 65536 (64 KiB UTF-8) | checkpoint `state` |
| `JSON_MAX_DEPTH` | 16 | checkpoint, signal, decision preview |
| `JSON_MAX_NODES` | 4096 | checkpoint, signal, decision preview |
| `SIGNAL_MAX_BYTES` | 8192 (8 KiB) | wakeup signal payload |
| `SUMMARY_MAX_CHARS` | 512 | safe owner-facing step summary |
| keys | must be `str` | every object |
| numbers | finite only | NaN/±Inf refused |

Ordinary Run input bounds are **unchanged** and still apply to the step's Run
`input_text`: `input_max_bytes=8000`, `input_max_code_points=4000` by default.
A checkpoint is not input text; it is a separate bounded column reached through
the SDK snapshot, never smuggled through `runs.input_text`.

### 3.3 Persistence (new tables)

`workflow_executions`
: `id`, `owner_user_id` FK, `agent_instance_id` FK, `submission_key` (opaque,
  owner-scoped), `submission_digest` (sha256 of canonical creation content),
  `workflow_kind`, `state_schema_version`, `status`, `paused`,
  `checkpoint_revision`, `step_count`, `budget_*`, `reserved_*`,
  `wait_kind`/`wakeup_at`/`signal_key`/`decision_key`, `deadline_at`,
  `review_reason`, pinned identity (`agent_key`, `agent_definition_version`,
  `model_provider`, `model_name`, `package_content_digest`,
  `package_environment_digest`, `effective_config_digest`,
  `agent_instance_config_revision`), `created_at`, `updated_at`, `finished_at`.
  Unique `(owner_user_id, submission_key)`.

`workflow_steps`
: `id`, `workflow_id` FK, `step_number`, `run_id` FK **nullable at creation,
  set in the same transaction as the Run insert** (it is set inside that
  transaction, never afterwards), `expected_checkpoint_revision`,
  `state` (the frozen checkpoint handed to the package), `state_digest`,
  `state_version`, `status`, `summary`, `created_at`, `finished_at`.
  Unique `(workflow_id, step_number)`, unique `(workflow_id, run_id)`,
  unique `run_id`.

`workflow_checkpoints`
: `id`, `workflow_id` FK, `revision`, `state_json`, `state_digest`,
  `step_number` (the step that produced it), `created_at`.
  Unique `(workflow_id, revision)`.

`workflow_signals`
: `id`, `workflow_id` FK, `owner_user_id` FK, `signal_key`, `payload_digest`,
  `expected_revision`, `received_at`, `accepted_at`, `outcome`.
  Unique `(workflow_id, signal_key)`.

`workflow_decisions`
: `id`, `workflow_id` FK, `owner_user_id` FK, `checkpoint_revision`,
  `tool_definition_id` FK, `upstream_name`, `action_fingerprint`,
  `arguments_digest`, `preview_json`, `package_content_digest`,
  `effective_config_digest`, `state`, `requested_at`, `expires_at`, `decided_at`,
  `consumed_at`. Unique `(workflow_id, checkpoint_revision, tool_definition_id)`.

### 3.4 Result transport (additive, no existing wire message changes)

`host_hello.payload.features` gains `"workflow-v1"`. An old host that does not
advertise it is refused **before `initialize`**, so package code never runs.

`run_request.payload.workflow` (optional, absent for ordinary Runs):
`{"state_version": 1, "checkpoint_revision": 3, "step_number": 2, "state": {...}}`.

`run_result.payload.workflow` (optional):
`{"directive": {...}, "state": {...}, "summary": "..."}`.

Both sides keep `MAX_FRAME_BYTES` (1 MiB) enforcement; the checkpoint bound is a
*stricter* 64 KiB check applied by both the host and the Worker before the frame
is written, so an oversized checkpoint cannot consume the frame budget.

## 4. The one commit boundary

```
PackageExecutionAdapter.run()
  └─ bounded result validated ──► ChatOutcome.workflow_step
       └─ RunExecutor ──────────► ExecutionOutcome.workflow_step
            └─ JobExecutionService._terminalize
                 └─ SqlAlchemyJobExecutionPersistence.succeed(..., workflow_step=…)
                      └─ ONE transaction:
                         1. Attempt RUNNING→SUCCEEDED   (fenced on claim token + lease)
                         2. Job      RUNNING→SUCCEEDED  (fenced)
                         3. Run      RUNNING→SUCCEEDED  (fenced)
                         4. capture_proposals(...)
                         5. workflow step RUNNING→succeeded + summary
                         6. workflow checkpoint revision N → N+1 (insert)
                         7. workflow status/progress/wait/reservation updates
                         8. run.succeeded event
```

There is **no** independent mid-step checkpoint API and no post-hoc write path.
If step 6 fails, 1–5 roll back too, so the Run is never `succeeded` with no
checkpoint. `succeed` returns `False` (fence lost) and nothing is committed.

A stale Attempt cannot checkpoint: step 5–7 compare
`workflow_steps.expected_checkpoint_revision == workflow_executions.checkpoint_revision`
and `workflow_steps.id == <run's linked step>` on the same connection.

## 5. Continuation submission

The Scheduler tick calls the **canonical** `insert_run_and_job_on_connection`
inside one `BEGIN IMMEDIATE` transaction that also writes the step row, links the
Run, and draws the reservations. It never inserts a Run by any other route.

Uncertain commits are reconciled from durable identity: `workflow_steps.run_id`
plus the unique `(workflow_id, step_number)` constraint. A retry re-reads the
existing row rather than inserting a second Run.

`max_attempts = 1` for every workflow Job. An Attempt that fails is terminal; a
dead Run projects to `needs_review`, never to an automatic new step.

Before accepting a continuation the tick verifies, on the same connection:
owner, enabled instance, exact definition identity, unchanged pinned
package/config/model identity, `deadline_at > now`, not paused, not cancelled,
`checkpoint_revision` matches the step's expected revision, and remaining
reservations cover the per-Run limits. A changed pinned identity is
`needs_review(configuration_changed)`, never an automatic re-pin.

## 6. Owner decisions are not H3 approvals

`workflow_decisions` authorizes *proposing* the next step. It grants nothing.

At dispatch time the ordinary Stage-H chain still runs, unchanged: grant cutoff →
tool permission evaluator → account connection ownership/scope/state → publisher
trust → and, for a live external action, a **fresh** `action_approvals` row bound
to the **new** `attempt_id`. A terminated Attempt's approval is never reused.

Ambiguity: `tool_invocations.status = 'ambiguous'` (provider may have succeeded)
projects the workflow to `needs_review(ambiguous_external_effect)`. The
reconciliation path is a **connector-declared safe read only**. Gmail declares
`list`, `search` and `get` safe to reconcile with; no write is retried.

## 7. Failure matrix

| Boundary | Durable state | Recovery |
|---|---|---|
| Before Run/step commit | nothing | retry same `(owner, submission_key)`; unique constraint makes a duplicate impossible |
| After Run/step commit, before response | step row + `run_id` | read the existing row; submit no second Run |
| During leased execution | Run `running` | existing Run recovery (lease reclamation); no workflow replay |
| Package success, Worker lost before persist | nothing durable | the Run is recovered as `failed`; workflow → `needs_review` |
| Provider effect then process loss | `tool_invocations` = `ambiguous` | `needs_review`; safe read only; never a blind write retry |
| Success + checkpoint committed, before wakeup | revision N+1 committed | tick submits the unique next step |
| Duplicate signal | identical `(signal_key, payload_digest)` | return the prior outcome, no second advance |
| Changed signal content, same key | digest mismatch | reject; no advance |
| Stale/out-of-order signal | `expected_revision` mismatch | reject; cannot advance the wrong wait |
| Pause races wakeup | serialized `BEGIN IMMEDIATE` | whichever commits first wins; a later tick sees `paused` and does not dispatch |
| Cancel races wakeup | serialized `BEGIN IMMEDIATE` | `cancelled` is terminal; a dispatch that lost the race never commits |
| Config/package/model changed | pinned digest mismatch | `needs_review(configuration_changed)` |
| Grant/account/trust revoked while waiting | unchanged workflow row | the *dispatch* is refused by the live checks; workflow → `needs_review` |
| Deadline passed | `deadline_at <= now` | `failed(deadline_exceeded)`; no dispatch |
| Checkpoint too large/deep/many nodes/NaN/non-string keys | — | validation fails **before** terminalization, so the Run cannot be stranded `succeeded` |

## 8. Security boundaries restated

- A package never receives owner id, Run/Attempt authority, lease tokens,
  credentials, or a database handle.
- A checkpoint is private application data. It is never promoted into context
  instructions, never becomes a memory item, and never grants a tool.
- Public health/audit projections never include checkpoint content, token
  values, or private mail content. Full state is owner-inspection only.
- Grants, accounts, and trust are rechecked live at every dispatch. Installing a
  package or connecting an account grants nothing automatically.

## 9. Explicit non-claims

- No exactly-once remote effect is claimed for any provider.
- No hard input-token budget is claimed. Input/context **byte** bounds remain
  authoritative; reported token usage is observational.
- No framework-native pickle/checkpointer, subgraph, or serialized interrupt is
  supported.
- No automatic migration of a running workflow onto a new package/config/model.
- No second queue, no alternate lease engine, no permanent per-agent process.