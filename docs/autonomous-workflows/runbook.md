# Autonomous workflows — operations runbook

How to run, inspect, and recover the durable autonomous workflow runtime delivered by
milestones W0–W6 under [ADR 0039](../adr/0039-durable-autonomous-workflows.md).

The frozen contract lives in [workflow-contract.md](workflow-contract.md). The published
LangGraph behaviour this adapter depends on is recorded in
[langgraph-compatibility.md](langgraph-compatibility.md). This document is operational only:
it says how to drive the system, not what it is allowed to decide.

---

## 1. Platform prerequisite — read this first

**The package demos and the Stage-G E2E journey require a supported Linux deployment.**

`create_containment()` dispatches only on Linux. On every other platform it raises
`ContainmentUnavailable`, and both the API's package health check and the Worker's package
execution refuse the operation **before any package code runs**. That refusal is deliberate
and frozen: an unsupported platform must never receive a silently uncontained adapter, so
"Package containment is unavailable on this platform; execution is refused" is the correct
output on Windows, not a defect to work around.

Concretely, on a non-Linux host:

- installing a `.nervos` package fails with `containment_unavailable` (HTTP 503);
- the Stage-G browser journey verifies the explicit install refusal; actual package
  execution is qualified separately on Linux;
- the `research-workflow` and `mail-triage` demos cannot execute.

Nothing about the workflow domain, the API, the SDK port or the Gmail connector is
platform-limited. Only **package** execution is. A workflow whose agent is a built-in
`nervos.chat` instance runs on any supported host.

Do not "fix" this by dispatching to a non-Linux adapter without an explicit owner
decision and an ADR; see [ADR 0035](../adr/0035-package-sandbox-and-resource-controls.md) for
the platform containment tiers that decision would have to weigh.

## 2. Building the demo packages

The two demo `.nervos` archives are **generated deterministically** and committed under
`artifacts/demos/`. They are build outputs, not hand-authored binaries:

```bash
uv run python scripts/build_autonomous_workflow_demos.py
```

| Archive | Bytes | Kind used at creation | Tools declared |
|---|---|---|---|
| `artifacts/demos/research-workflow.nervos` | rebuild to inspect | `research` | mediated search evidence, then one model call |
| `artifacts/demos/mail-triage.nervos` | rebuild to inspect | `mail_triage` | `gmail.users.messages.list`, `gmail.users.messages.get` |

`workflow_kind` is supplied by the operator when the workflow is created; the archive does not
pin it. The values above are the ones the acceptance test drives.

Both declare the `workflow-v1` runtime-integration capability in their manifest. The mail
demo declares no send capability. It prepares a review preview and proposes one exact
`gmail.users.messages.get` re-read. Approval resumes that safe read; declining cancels
the workflow. No mail is sent. Set its `read_tool_definition_id` configuration to the
actual granted native Gmail get definition, rather than an assumed numeric ID.

Re-running the builder must produce byte-identical archives — signing uses a fixed Ed25519
seed and the wheel build embeds no timestamps, so a rebuild in the same environment is
reproducible. `packages/nervos-core/tests/integration/test_autonomous_workflow_demos.py`
asserts this along with both demos running on one runtime, neither sharing checkpoint state,
resumption across a restart, and a waiting workflow surviving one.

To confirm the committed archives are not stale, rebuild and compare:

```bash
md5sum artifacts/demos/*.nervos
uv run python scripts/build_autonomous_workflow_demos.py
md5sum artifacts/demos/*.nervos   # must be unchanged
```

## 3. Running the system

Four processes, in this order. Each is a plain `uv run python -m <package>`:

```bash
# 1. API (control plane)
uv run python -m nervos_api

# 2. Web dashboard (local)
pnpm --filter @nervos/web dev

# 3. Worker (execution plane)
uv run python -m nervos_worker

# 4. Scheduler (the ONLY thing that advances a workflow)
uv run python -m nervos_scheduler
```

The Scheduler is not optional for workflows. `WorkflowContinuationService.tick()` is called
from the existing Scheduler loop (`apps/scheduler/src/nervos_scheduler/main.py`), not from a
new queue and not from a second process. With no Scheduler running, workflows sit at their
current step indefinitely and the dashboard looks frozen; that is the expected reading, not
a hang.

Watch for the tick's own log line:

```
scheduler_workflow_tick examined=<n> dispatched=<n> deferred_capacity=<n>
```

`deferred_capacity` above zero means work was found but not dispatched because the queue was
full. That is backpressure doing its job — the work stays due and the next tick retries it.

### Bootstrap

```bash
uv run python scripts/bootstrap.py     # creates the venv and installs all workspace members
```

Never run a bare `uv sync` — it prunes the workspace venv and breaks `sqlalchemy`/`alembic`.
Use `uv sync --all-packages` if you invoke uv directly.

## 4. The workflow lifecycle from the operator's side

A workflow is created over HTTP and then advanced **only** by the Scheduler.

```bash
# Create (first step, its Run, and its Job are inserted in ONE transaction)
curl -sS -X POST http://127.0.0.1:8000/api/v1/workflows \
  -H 'Origin: http://localhost:5173' -b session.txt \
  -H 'Content-Type: application/json' \
  -d '{
        "agent_instance_id": 1,
        "submission_key": "research-2026-10-04-a",
        "input_text": "Summarise durable workflow checkpointing",
        "workflow_kind": "research"
      }'
```

`submission_key` is an **idempotency key scoped to the owner**, not a free-form label. It is
enforced by a unique `(owner, submission_key)` constraint, not by a pre-flight read, so two
concurrent identical submissions cannot both insert. Replaying the identical body returns the
**original** workflow; sending a *different* body under the same key returns
`409 workflow_conflict`.

All mutating routes require the exact configured `Origin` header. A missing or foreign
origin is `403 invalid_origin` before the session is even consulted.

### Controls

| Verb | Route | Meaning |
|---|---|---|
| Pause | `POST /workflows/{id}/pause {"paused": true}` | Stop accepting **future** steps. In-flight Runs still finish. |
| Resume | `POST /workflows/{id}/pause {"paused": false}` | Clear the pause flag. Fabricates nothing. |
| Cancel | `POST /workflows/{id}/cancel` | Terminate permanently. Does **not** cancel a live Run. |
| Signal | `POST /workflows/{id}/signals` | Owner wakeup. `expected_revision` is **required**. |
| Decide | `POST /workflows/{id}/decisions/{decision_id}` | Owner decision on proposed work. `expected_revision` is **required**. |
| Inspect | `GET /workflows/{id}/needs-review` | Recovery guidance derived only from recorded reasons. |

Pause and cancel are deliberately separate verbs. Pause is a durable flag, not a synthetic
signal, so resuming can never fabricate the wakeup the pause withheld. Cancelling a
terminal workflow — including pausing one — is `409 workflow_transition_conflict`.

Cancellation of a *live Run* is a separate, pre-existing authority. `POST /workflows/{id}/cancel`
does not reach it, and nothing in the workflow layer grants itself a way to do so.

### Signals: 202 with a named outcome, not a 4xx

A signal is `202` whether or not it advanced anything. The body says which:

```json
{"accepted": false, "outcome": "stale_revision", "workflow": { ... }}
```

Outcomes are `accepted`, `replayed`, `stale_revision`, `not_waiting`, `payload_conflict`,
and `cancelled`.

Two behaviours are easy to misread:

- **A refused signal is deliberately not recorded.** It does not occupy the
  `(workflow, signal_key)` identity, so a stale-revision probe or a wrong-key probe cannot
  burn the name and block the legitimate delivery that follows. The refusal is returned
  synchronously; the durable signal table only ever holds *accepted* deliveries.
- **`replayed` only applies to an accepted signal.** Re-delivering an accepted signal returns
  `accepted: true, outcome: "replayed"` and does not advance twice. Re-delivering a *refused*
  signal re-evaluates and returns its own outcome again, because there is no prior row.

Changed payload content under an already-accepted key is `409 workflow_conflict`.

### Owner decisions are not H3 approvals

A workflow decision authorizes *proposed work* to proceed to its own dispatch boundary, where
live tool grants, account state and cancellation are rechecked. It is not the Stage-H H3
live-dispatch approval, and it does not pre-authorize a remote effect.

## 5. What the dashboard shows, and what it deliberately does not

`/workflows` lists workflows; the detail view shows steps, budgets and recovery guidance.

The projection rules are the security-relevant part:

- **The list projection carries no checkpoint content at all.** A workflow list is a summary;
  application state is not a summary.
- **The detail projection carries checkpoint _shape_, never values** — `revision`,
  `step_number`, `byte_size`, sorted top-level `keys` (max 32), and `created_at`. An owner can
  confirm a step stored what they expected without the dashboard becoming a renderer for
  arbitrary application state.
- **Inspect checkpoint** explicitly fetches owner-only state from
  `GET /workflows/{id}/checkpoints/{revision}`. It renders escaped text, never HTML,
  permissions or executable objects. The query belongs to the private workflow cache.
- Signal waits expose a bounded JSON-object form. The resumed SDK snapshot receives
  `wake_signal` with the accepted key and payload for that exact checkpoint revision.
- Proposed actions show the preview and expiry. The decision's revision, rather than
  the later checkpoint revision, binds approval. Expired approved decisions cannot
  dispatch; successful continuation admission consumes the decision transactionally.
- **Recovery guidance is derived only from the reason the runtime actually recorded.** An
  unmapped reason is reported as unknown rather than papered over with generic advice.

The API returns `404 workflow_not_found` for both "no such workflow" and "belongs to another
owner", identically. The response cannot be used to discover whether another owner's workflow
id exists.

## 6. Recovery

### A workflow is not advancing

1. Is the Scheduler running? Look for `scheduler_workflow_tick` in its log.
2. Is it `paused`? `GET /workflows/{id}` — `paused: true` means steps are withheld by design.
3. Is it `waiting`? Check `wait_kind`:
   - `time` — `wakeup_at` says when; nothing to do.
   - `signal` — deliver the signal the workflow is waiting for, at the **current**
     `checkpoint_revision`.
   - `owner_decision` — open the pending decision and decide it.
4. Is a step's Run failing? Check the Run's own timeline. A failing step consumes its budget
   and the workflow moves to `needs_review` when exhausted.

### `needs_review` reasons

| `review_reason` | What happened | What to do |
|---|---|---|
| `deadline_exceeded` | Passed its deadline unfinished | Start a new workflow with a longer budget |
| `step_limit` | Used every allowed step | Start a new workflow with a larger step budget |
| `budget_exhausted` | Exhausted reserved model/tool/output budget | Start a new workflow with a larger reservation |
| `configuration_changed` | Agent's package or configuration changed after start | Start a new workflow; it will not resume against a new identity |

A workflow **never** migrates to a new package or configuration version automatically. That is
an explicit owner operation, because resuming against a changed identity is how a checkpoint
starts replaying against different code.

### Restart mid-step

Checkpoints are committed inside the step's fenced transaction, so a crash leaves the
workflow at a real step boundary. On restart the Scheduler picks up the remaining due work;
a workflow that was `waiting` is still `waiting`, because the wait is durable state rather
than in-process memory. `test_autonomous_workflow_demos.py` asserts restart resumption and
waiting-survives-restart directly.

## 7. The Gmail connector (read-only)

`packages/nervos-mcp/.../connectors/gmail.py` exposes a **read-only** Gmail connector:

- scope is exactly `GMAIL_READONLY_SCOPE` — no send, no modify, no trash;
- the API origin is pinned, and the egress policy is **re-validated immediately before every
  dial**, not once at construction;
- list and get are both bounded;
- transport, configuration and protocol failures are all classified into
  `ToolExecutionFailure` rather than escaping as raw exceptions.

There is one list operation with an optional `query`. A separate `SEARCH_MESSAGES` operation
would have duplicated `LIST_MESSAGES` and left search unreachable, so it was collapsed.

Headers are canonicalized on both storage and lookup, so a message's `Subject` is actually
populated rather than silently empty.

Caller-supplied identifiers and provider-returned identifiers are validated separately: a
caller-supplied id that is malformed is a `ValueError` (a client mistake), while a
provider-returned id that is wrong is a transport error (a remote fact). Conflating them
would have let a caller mistake be reported as a provider fault.

**Sending is out of scope for this connector.** The mail demo proposes a safe re-read
for owner review and never sends, drafts in Gmail, deletes or modifies messages.

### Configuring native Gmail reads

1. Run a migrated API, Worker and Scheduler against the same local database. Worker
   startup reconciles `gmail.users.messages.list` and `gmail.users.messages.get` after
   validating the schema. Registration grants nothing.
2. Configure the existing H2 OAuth provider in `NERVOS_ACCOUNT_OAUTH_PROVIDERS` on API
   and Worker: a Google client ID, authorization/token endpoints, the exact registered
   NervOS callback URI, and only `https://www.googleapis.com/auth/gmail.readonly`.
   Follow the Stage-H OAuth setup for client-secret custody and encrypted master keys.
   Connect the account through Security → connected accounts. Tokens stay encrypted.
3. Add `https://gmail.googleapis.com` to the Worker's `NERVOS_MCP_ALLOWED_ORIGINS`.
4. Inspect the two durable tool IDs and fingerprints in Tools & permissions. Create
   one operator-reviewed `NERVOS_ACCOUNT_TOOL_BINDINGS` item per definition, with
   `tool_definition_id`, `account_connection_id`, exact `fingerprint`,
   `required_scope` set to the read-only scope, and `endpoint` set to that origin.
   Omit `mcp_connection_id` for these native definitions. Restart the Worker.
5. Install the mail package on Linux. Bind `gmail.messages` to native list and
   `gmail.message` to native get, then grant both explicitly. Set
   `read_tool_definition_id` to the get ID in package configuration. Start a workflow.

The Worker checks the binding, fingerprint, live owner/account/scope and grant on every
dispatch. Missing bindings fail closed. The HTTP client uses bounded GET requests,
refuses redirects and ambient proxies, and cannot dial another origin or mailbox.
Tests use a deterministic remote transport; no live Google account is claimed qualified.

## 8. The `workflow-v1` host feature and the LangGraph adapter

A package opts in by declaring exactly, in its manifest:

```yaml
x-nervos-runtime-integration:
  workflow: workflow-v1
```

The value must be that **exact string**. `true`, and any unknown value, are rejected at parse
time rather than treated as "probably fine".

The Worker refuses a `workflow-v1` package **before `initialize`** when the host does not
advertise the capability, and the refusal is made host-side: the host binds the revision,
digests and expiry, so a package cannot assert its own authority to continue.

### Using the pinned adapter

`packages/nervos-langgraph` pins **`langgraph==1.2.12`** and qualifies that version
exclusively; `assert_qualified_langgraph()` refuses anything else.

```python
from nervos_langgraph import LangGraphStepRunner  # compiled with NO checkpointer
```

Verified against real LangGraph 1.2.12, and published in
[langgraph-compatibility.md](langgraph-compatibility.md):

- **Without a checkpointer, `interrupt()` does not raise.** It returns `__interrupt__` and
  silently discards the node update. `LangGraphStepRunner` therefore raises
  `FrameworkDurableWait` on `__interrupt__`, and is compiled with **no checkpointer** so that
  this silent-discard behaviour cannot occur.
- `recursion_limit` surfaces as `GraphRecursionError`, mapped to `StepBudgetExceeded`.
- `Durability` accepts exactly `('sync', 'async', 'exit')`.

Architecture guards in `packages/nervos-core/tests/architecture/test_boundaries.py` forbid the
LangGraph adapter from importing a checkpointer, a saver, `pickle`, or any LangChain model
client, and require it to reach the SDK only through public NervOS interfaces.

## 9. Verification

```bash
uv run pyright                                   # 0 errors, 0 warnings
uv run ruff check packages apps scripts
uv run pytest -q                                 # or: uv run --with pytest-xdist pytest -q -n 8
uv run python scripts/check.py security
pnpm run typecheck && pnpm run lint && pnpm run test && pnpm run build
uv run python scripts/check.py e2e              # Linux required for stage-g; see §1
```

Migration head: **`0024_durable_workflows`**.

Workflow-specific suites:

| Suite | Covers |
|---|---|
| `packages/nervos-core/tests/integration/test_autonomous_workflow_demos.py` | both demos, shared runtime, restart resumption |
| `apps/scheduler/tests/integration/test_scheduler_workflow_tick.py` | the only advancing path |
| `apps/api/tests/integration/test_workflow_routes.py` | the owner-facing wire contract |
| `apps/worker/tests/unit/test_workflow_v1_host_feature.py` | host capability and step finalization |
| `packages/nervos-langgraph/tests/test_adapter_qualification.py` | pinned-version behaviour |
| `packages/nervos-mcp/tests/test_gmail_connector.py` | read-only egress and bounds |

Two operational notes for anyone extending these:

- The Scheduler tick reads the **real clock**. Test fixtures must build workflows relative to
  `datetime.now(UTC)`, not to a fixed past date, or every fixture workflow is already
  deadline-expired.
- `SqlAlchemyJobExecutionPersistence.succeed(..., workflow_steps=...)` returns `False` unless
  a `SqlAlchemyWorkflowStepFinalizer` is supplied. A test that composes the persistence
  directly and then asserts a step advanced will see it silently not advance.

## 10. Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| Workflow stuck at step 1 | Scheduler not running | Start `nervos_scheduler` |
| `deferred_capacity` climbing | Queue backpressure | Expected; work stays due and retries next tick |
| `409 workflow_conflict` on signal | Stale `expected_revision` | Re-read the workflow and use the current revision |
| `409 workflow_conflict` on create | Same `submission_key`, different body | Use a new key, or resend the identical body |
| `403 invalid_origin` | Missing/foreign `Origin` header | Send the exact configured origin |
| `503 containment_unavailable` | Non-Linux host | See §1 — deploy on Linux |
| `409 workflow_transition_conflict` | Terminal workflow | Expected; start a new workflow |
