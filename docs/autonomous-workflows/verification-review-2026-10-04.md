# W0–W6 verification review — 2026-10-04

## Conclusion

Historical audit: these reproduced defects were repaired and qualified in the
[2026-10-05 closeout](verification-closeout-2026-10-05.md).

The prompt has been substantially implemented, but **W0–W6 are not completely
integrated or ready to be declared working**. The completion statement in
`docs/implementation-status.md` is contradicted by the reproduced failures below.
Passing the existing tests does not establish the missing acceptance behavior.

This task is an audit, not implementation of the repairs. No production code was
changed during this audit. Probes used disposable migrated databases and fake
tools; no real model, mailbox, credentials or developer database was used.

## What exists

| Milestone | Code/documentation found | Audit assessment |
|---|---|---|
| W0 | ADR 0039 and workflow-contract.md | Documented; adapter behavior differs from approved per-node contract |
| W1 | Domain/services, five workflow tables, migration 0024, Worker success hook | Implemented; success path tested, lifecycle integration incomplete |
| W2 | Scheduler tick, continuation insertion, waits, signals, budgets | Incomplete: failed Runs remain running; identity refusal leaves orphan Runs |
| W3 | Durable decisions and separation from H3 approval | Partial; full external-effect recovery acceptance missing |
| W4 | workflow-v1 SDK/host and LangGraph 1.2.12 adapter | Partial qualification; no enforced checkpointer refusal or per-node continuation |
| W5 | Owner API, Workflows UI, Gmail read connector class | Partial: no production Gmail transport/Worker wiring; UI gaps |
| W6 | Generated research/mail archives and persistence tests | Incomplete: stub research, broken mail parsing, no actual workflow package acceptance |

## Reproduced runtime blockers

### 1. A failed Run leaves its workflow permanently running

Location: `packages/nervos-core/src/nervos_core/infrastructure/database/workflows.py`,
`SqlAlchemyWorkflowContinuationPersistence._one`; failure finalization in jobs.py.

An isolated database was migrated to `0024_durable_workflows`. A workflow's Run
was claimed, started and failed using the real Job persistence, then the real
workflow Scheduler tick was called. Observed:

```json
{"run_status":"failed","workflow_status":"running","workflow_review_reason":null,"tick_examined":0}
```

The tick selects only pending/runnable/waiting workflows; failure and lease-loss
paths do not project terminal Runs into workflow progress. This breaks W2/W3
failure recovery and truthful operator status. The same integration must cover
Run cancellation and lease expiry. A second probe also reproduced a successful
builtin/legacy result without a workflow directive leaving the workflow running
at revision zero, with zero rows examined by the Scheduler.

Repair direction: idempotent bounded terminal-Run reconciliation, or complete
transactional hooks across all terminal paths. Preserve ordinary Run fencing and
never replay an uncertain effect to rebuild progress.

### 2. Refusing changed configuration still accepts an unlinked Run

Location: `submit_continuation_on_connection` inserts a Run/Job before
`_assert_identity_unchanged`; `_advance` catches `_Uncertain` in that transaction.

After a successful next-step checkpoint, the instance's model name was changed
in the disposable fixture. The real tick reported:

```json
{"workflow_status":"needs_review","orphan_runs_after_tick":1,"tick_dispatched":0}
```

Because the exception is caught without rolling back the insertion, a queued Run
and Job survive with no workflow step. A Worker can execute unwanted work after
the workflow refused continuation. The existing drift test checks needs_review
but not absence of additional Runs/Jobs.

Repair direction: validate pinned identity before insertion on the same connection,
and make refusal atomic using an appropriate rollback/savepoint if post-insertion
validation is necessary. Test exact Run/Job counts, budget and linkage after refusal.

### 3. Mail demo discards valid immutable SDK tool results

Location: `scripts/build_autonomous_workflow_demos.py`, generated MailTriageAgent.

The SDK freezes ToolResult.content into an immutable mapping and JSON arrays into
tuples. The demo tests for `dict` and `list`, so it discards real SDK-shaped results.
Executing the generated application code against an ordinary SDK fake ToolPort
returning one unread message produced:

```json
{"tool_calls":["gmail.messages"],"response":"No unread mail to triage yet.","wait_kind":"time","classified_count":0}
```

Repair direction: consume public Mapping/Sequence shapes, check tool errors, parse
the actual bridge result envelope, read the message and checkpoint its classification.
Use real SDK immutable ToolResult in regression tests, then rebuild archives.

## Remaining integration and acceptance gaps

- **Gmail is a class with fake transport tests, not an operational connected app.**
  `GmailTransport` has no concrete HTTP implementation; `GmailReadConnector` is not
  composed into the production Worker. No tool registration/account-selection path
  wires these reads into a running package. Add bounded transport, registration,
  broker/dispatch wiring and operator OAuth/grant setup; qualify the complete path.
- **Research demo fabricates sources.** It calls a tool but ignores the response,
  appends `source-N` placeholders and joins them into a brief. It never calls the
  model. W6 requires retaining mediated evidence and producing a model-generated
  brief with source provenance.
- **Demo tests bypass the applications and package host.** They seed a builtin
  chat instance and call `SqlAlchemyJobExecutionPersistence.succeed` directly with
  invented workflow results. They do not execute the generated agents, LangGraph,
  Gmail connector or production containment. Keep them as persistence tests, but
  add actual installed package -> Worker -> host -> ports -> checkpoint journeys.
- **LangGraph contracts are not enforced.** The adapter invokes the entire graph
  (default recursion limit 32) rather than one node per Run in ADR 0039. Its prose
  claims to refuse checkpointers, but run() does not check the compiled graph's
  checkpointer. A real LangGraph graph compiled with InMemorySaver and configured
  with a thread ID was accepted by the adapter and wrote **three native framework
  checkpoints** in the isolated probe. Freeze a reviewed contract and enforce it; qualify state, model/tool
  identity and wait behavior through package IPC, not only direct graph invocation.
- **Workflow dashboard is unfinished.** Creation hardcodes research; list/detail
  query hooks have no polling; the page offers no signal delivery control; run
  buttons navigate to `/runs/{id}`, for which the router has no route. Decision
  previews are returned by the API but not rendered before Approve. Complete these
  flows and add actual browser acceptance with keyboard/contrast/outage checks.
- **Bounded scan is not fair pagination.** Each tick reselects the first 200 due
  rows. Signal/decision waits with no wakeup_at remain eligible and can obscure
  later work. Use a durable ordering/appropriate cursor and test a blocked prefix.
- **Output-token reservations undercount multi-call steps.** `reservation_for` in
  application/workflows.py reserves `max_output_tokens` once; the package model
  bridge applies that limit per model call. A step permitted eight 1024-token
  completions reserves only 1024 rather than its 8192-token maximum. Reserve the
  entire permitted capacity for both initial and continuing steps, or enforce a
  reviewed aggregate per-step cap through all model calls.
- **Full-state inspection, cleanup/retention and complete failure matrix acceptance
  need evidence.** Key names alone do not prove the application stored correct
  results. Separate private state inspection from public/audit projections.

## Fresh verification evidence

- Frontend Vitest: **172 passed, 20 test files**.
- Frontend production build: **passed**.
- Frontend ESLint: **passed**.
- Pyright: **0 errors, 0 warnings**.
- Ruff lint: **passed**.
- Ruff format check: **failed — 17 files need formatting**.
- Security scan: **809 files, no findings**; this is a static scan, not proof of
  broker/isolation correctness.
- Focused core workflow, LangGraph and Gmail tests: **59 passed**.
- Extended workflow/API/Scheduler/host/migration test group: **178 passed** in a
  clean temporary Python environment.
- Full Python regression suite (`pytest -n 8 -q`): **3482 passed, 5 skipped,
  1 failed**, in 796.62 seconds. Failure:
  `test_job_execution_service.py::test_success_persists_one_terminal_run_with_one_provider_call`
  expected elapsed_ms=0 but observed 32 under parallel load. An isolated rerun
  **passed**; this indicates timing sensitivity, not a clean full-suite pass.
  Inject a deterministic monotonic clock for exact elapsed-time assertions, or
  test the elapsed-time contract without requiring real time to equal zero.
- Existing browser journeys: **failed the 120-second supervisor timeout**. Setup/
  authentication and automation journeys passed before timeout; the entire four-
  journey group did not finish. No workflow journey exists under apps/web/e2e.
- Linux qualification on this host: **12 prerequisite skips, 0 properties passed**
  because the host is Windows. This is not Linux qualification. No new Linux
  workflow-package acceptance was performed.

The initial normal `uv run` checks attempted dependency synchronization and failed
on a locked `websockets` binary. The normal environment then failed Gemini SDK
import (`websockets.ConnectionClosed` missing). Verification continued in a fresh
temporary environment created with `uv sync --all-packages --frozen` and commands
using `--no-sync`. The locked binary was then proved byte-identical to the clean
locked-version installation; missing package source/metadata were restored without
overwriting that binary. Normal-environment Gemini SDK import now succeeds with
websockets 16.1.1. No running user service was stopped. The full serial regression
run was deliberately stopped and restarted with eight pytest-xdist workers in the
temporary audit environment; the repository dependency files were not changed.

## Repair order before completion

1. Fix refusal atomicity and terminal-Run/workflow reconciliation with regression
   tests proving no extra execution and no permanently running terminal workflow.
2. Complete SDK/framework contract enforcement and signal/fairness lifecycle behavior.
3. Wire the real read-only Gmail connector through the existing broker and mediator.
4. Correct both demos and execute them as actual packages in the qualified sandbox.
5. Complete UI controls/previews/live status and workflow browser acceptance.
6. Run complete migration, regression, static, security and supported-Linux gates.
7. Replace premature W0–W6 completion claims with evidence-based status and runbook.
