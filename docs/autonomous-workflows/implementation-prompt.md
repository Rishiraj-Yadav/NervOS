# NervOS — W0–W6 autonomous workflow implementation prompt

Copy the prompt below into the implementation chat running in this repository.

---

You are continuing the owner-approved NervOS autonomous workflow implementation in:

`R:\7_sem\Major-Project\Nervos-self-hosting`

Implement W0–W6 in order and qualify the complete result. This is a general runtime
foundation for multiple kinds of autonomous applications, not only an email agent.
Research and mail triage are demonstrations of that foundation.

## 1. Authorization, starting point and working rules

The owner approved `docs/autonomous-workflows/implementation-plan.md` and asked for
implementation. Execution was interrupted after documentation changes only:

- The plan now records owner authorization for W0–W6 on 2026-10-04.
- `docs/adr/0039-durable-autonomous-workflows.md` was added as the implementation contract.
- No workflow runtime code, workflow migration, SDK workflow extension, framework
  adapter, mail connector or Workflows dashboard was implemented in that execution.
- No W acceptance tests have been run. W0 interface/governance acceptance still
  needs to be checked against the inspected repository contracts.

Confirm this starting point using the worktree before acting. Newer work may exist.
Preserve all existing changes: the worktree already contains many modified and
untracked Stage-H and runtime-integration files. They are required predecessor
work, not disposable generated files. Do not reset, stash away, delete, overwrite
or revert unrelated changes. Inspect relevant diffs before editing shared files.

Do not commit, push, create a PR, merge, deploy, publish a package to a real
Marketplace or operate a real mailbox without a separate request. Build local
reviewable examples and isolated acceptance fixtures. Real email send/delete is
outside this first connector.

Follow root and applicable nested `AGENTS.md` instructions. Use the relevant
runtime-feature, db-migration, add-api-endpoint, add-mcp-tool, add-agent,
test-change and security-review skills when their work starts. Use package-agent
for the explicitly requested example-package build/qualification. Read each
applicable skill before applying it.

Never read real `.env`, `.env.*`, private keys, credential files, `secrets/` or
the developer's live database. Fake `.env.example` values may document settings.
Do not put secrets in checkpoints, memory, model context, manifests, logs or API
responses. Do not print environment dumps. Automated checks use temporary data.

Keep progress updates concise and visible. Work through ordinary implementation
problems without repeatedly asking for choices already settled by this plan.
For a material conflict with a frozen invariant, describe the concrete conflict
and the proposed successor decision; do not weaken security or claim acceptance.
Do not promise zero defects: prove behavior with recorded tests and state limits.

## 2. Objective and success criteria

Deliver a durable agent application runtime in which:

1. A WorkflowExecution retains private application progress across processes.
2. Each step executes as an ordinary Run -> Job -> Attempt -> Worker.
3. Steps checkpoint only through the fenced Worker finalization boundary.
4. Waiting workflows use no Worker execution slot or permanent package process.
5. The existing Scheduler submits due continuations through the canonical
   Run/Job insertion, with the existing admission, fairness and concurrency rules.
6. Duplicate events, restarts and uncertain commits cannot create duplicate steps.
7. Failed/ambiguous external actions never get blindly replayed.
8. Model and tool calls use NervOS ports; accounts and tokens stay in the broker.
9. A narrow pinned LangGraph integration is qualified, with truthful compatibility.
10. The dashboard lets owners inspect progress, waits, Runs, decisions, budgets,
    pause/resume/cancel and recovery requiring attention.
11. A research workflow and read-only mail-triage workflow work through the same
    production package-host path on a qualified Linux platform.
12. Relevant regression, migration, static, security and browser gates pass before
    authoritative implementation status changes to delivered.

Workflow state is not conversation history or scoped memory. Stage J delegation,
shared workspaces and cross-agent memory remain separate, unauthorized future work.

## 3. Required reading — in order

All paths below are relative to the repository root given above. These are real
existing starting files; proposed new files appear separately in section 5.
Read the implementation rather than inferring delivery from roadmap prose.

### A. Authority and approved scope

- `AGENTS.md`
- `docs/implementation-status.md`
- `docs/autonomous-workflows/implementation-plan.md`
- `docs/adr/0039-durable-autonomous-workflows.md`
- `docs/architecture.md`
- `docs/runtime.md`
- `docs/permissions.md`
- `docs/roadmap.md`
- `docs/database.md`
- `docs/configuration.md`

Resolve stale delivered-state wording using actual evidence. The status document
currently has some older Stage-H sentences alongside newer qualification results;
do not use those old sentences to undo the delivered security boundaries.

### B. Execution, schedules and side-effect authority

- ADRs 0009–0014: durable submission, liveness, retry, cancellation, concurrency
  and observability. Locate the exact filenames under `docs/adr/`.
- ADRs 0015–0017: grants, MCP trust and ambiguity without replay.
- ADRs 0018–0020: scheduling, ingress idempotency and canonical acceptance.
- `packages/nervos-core/src/nervos_core/domain/runs.py`
- `packages/nervos-core/src/nervos_core/domain/jobs.py`
- `packages/nervos-core/src/nervos_core/domain/execution.py`
- `packages/nervos-core/src/nervos_core/application/agents.py`
- `packages/nervos-core/src/nervos_core/application/agent_definitions.py`
- `packages/nervos-core/src/nervos_core/application/run_execution.py`
- `packages/nervos-core/src/nervos_core/application/trusted_chat.py`
- `packages/nervos-core/src/nervos_core/application/job_execution.py`
- `packages/nervos-core/src/nervos_core/application/lease_reclamation.py`
- `packages/nervos-core/src/nervos_core/application/run_cancellation.py`
- `packages/nervos-core/src/nervos_core/application/scheduler.py`
- `packages/nervos-core/src/nervos_core/application/triggers.py`
- `packages/nervos-core/src/nervos_core/infrastructure/database/jobs.py`
- `packages/nervos-core/src/nervos_core/infrastructure/database/transaction.py`
- `packages/nervos-core/src/nervos_core/infrastructure/database/triggers.py`
- `apps/worker/src/nervos_worker/app.py`
- `apps/worker/src/nervos_worker/service.py`
- `apps/worker/src/nervos_worker/registry.py`
- `apps/scheduler/src/nervos_scheduler/app.py`
- `apps/scheduler/src/nervos_scheduler/main.py`
- `apps/scheduler/src/nervos_scheduler/config.py`

Understand these seams before designing new persistence:

- `AgentService.prepare_submission` validates ownership, definition, provider and input.
- `insert_run_and_job_on_connection` in database/jobs.py is the one canonical
  insertion. It reads current enabled state, admission capacity, grant cutoff and
  execution/configuration snapshot on the caller's transaction connection.
- `TransactionRunner` uses short serialized transactions and retries only errors
  proven not to have committed. Unknown commit outcomes need reconciliation.
- `SqlAlchemyJobExecutionPersistence.succeed` fences Attempt/Job/Run completion
  and memory-proposal capture in one transaction. Workflow success belongs here
  or in an equivalent narrow hook on that same connection, not a later blind write.
- `ExecutionOutcome` and `ChatOutcome` carry package execution results.
- Scheduler composition is in app.py and its loop in main.py. There is no
  `apps/scheduler/src/nervos_scheduler/service.py`.

### C. Context, memory and package compatibility

- `docs/stage-f/README.md` and accepted ADRs 0021–0023.
  Before any Stage-F change, run `git hash-object docs/stage-f/README.md`, report
  the blob and verify predecessor completion. Previously inspected blob:
  `62cfc0a8039e233c82e2a30ff3fd59495e99395d`; recompute, do not assume unchanged.
- `docs/stage-g/README.md` and accepted ADRs 0024–0026.
- `docs/adr/0031-runtime-integration-context-memory-and-mcp.md`
- `docs/runtime-integration/README.md`
- `docs/agent-packages.md`
- `packages/nervos-core/src/nervos_core/domain/context.py`
- `packages/nervos-core/src/nervos_core/domain/memory.py`
- `packages/nervos-core/src/nervos_core/application/context_builder.py`
- `packages/nervos-core/src/nervos_core/application/runtime_integration.py`
- `packages/nervos-core/src/nervos_core/application/package_manifest.py`
- `packages/nervos-core/src/nervos_core/infrastructure/database/runtime_integration.py`
- `packages/nervos-core/src/nervos_core/infrastructure/database/conversations.py`
- `packages/nervos-core/src/nervos_core/infrastructure/database/memory.py`
- `packages/nervos-sdk/src/nervos_sdk/types.py`
- `packages/nervos-sdk/src/nervos_sdk/ports.py`
- `packages/nervos-sdk/src/nervos_sdk/entrypoint.py`
- `packages/nervos-sdk/src/nervos_sdk/__init__.py`
- `packages/nervos-package-host/src/nervos_package_host/wire.py`
- `packages/nervos-package-host/src/nervos_package_host/runner.py`
- `packages/nervos-package-host/src/nervos_package_host/__main__.py`
- `apps/worker/src/nervos_worker/package_execution.py`

### D. Security and account connector

- `docs/stage-h/README.md`
- `docs/stage-h/verification-review-2026-10-03.md`
- ADRs 0032–0038, particularly 0037 same-Attempt dispatch and 0038 pre-exec isolation.
- `packages/nervos-core/src/nervos_core/application/account_connections.py`
- `packages/nervos-core/src/nervos_core/application/account_oauth.py`
- `packages/nervos-core/src/nervos_core/application/account_actions.py`
- `packages/nervos-core/src/nervos_core/application/approvals.py`
- `packages/nervos-core/src/nervos_core/application/secrets.py`
- `packages/nervos-core/src/nervos_core/application/publisher_trust.py`
- `packages/nervos-core/src/nervos_core/application/tool_invocation_mediator.py`
- `packages/nervos-core/src/nervos_core/application/tool_invocations.py`
- Their persistence modules under `packages/nervos-core/src/nervos_core/infrastructure/database/`.
- `packages/nervos-mcp/src/nervos_mcp/account_tokens.py`
- `packages/nervos-mcp/src/nervos_mcp/executor.py`
- `packages/nervos-mcp/src/nervos_mcp/gateway.py`
- `apps/worker/src/nervos_worker/account_actions.py`
- `packages/nervos-core/src/nervos_core/application/sandbox.py`
- `packages/nervos-core/src/nervos_core/infrastructure/sandbox/linux_bubblewrap.py`

### E. Schema, API, dashboard and test tooling

- `packages/nervos-core/src/nervos_core/infrastructure/database/models.py`
- `apps/api/alembic/env.py` and the current local migration chain, especially
  `apps/api/alembic/versions/0023_worker_sandbox_capability.py`.
- `apps/api/src/nervos_api/app.py`
- `apps/api/src/nervos_api/api/dependencies.py`
- `apps/api/src/nervos_api/api/errors.py`
- `apps/api/src/nervos_api/api/router.py`
- `apps/api/src/nervos_api/api/routes/runtime_integration.py`
- `apps/api/src/nervos_api/api/routes/security.py`
- `apps/web/src/router.tsx`, `apps/web/src/routing.tsx` and `apps/web/src/styles.css`
- `apps/web/src/components/AppNavigation.tsx`
- `apps/web/src/components/RuntimeControls.tsx`
- `apps/web/src/components/RunItem.tsx` and `RunTimeline.tsx`
- `apps/web/src/pages/AgentInstancePage.tsx`
- `apps/web/src/pages/ConnectionsPage.tsx` and `SecurityPage.tsx`
- `apps/web/src/pages/RuntimeHealthPage.tsx`
- `apps/web/src/api/client.ts`, `queries.ts`, `runtimeIntegration.ts` and `security.ts`
- `apps/api/tests/integration/test_migrations.py` and `test_migrations_d1.py`
- `packages/nervos-core/tests/integration/execution_support.py`
- `packages/nervos-core/tests/integration/scheduler_support.py`
- Scheduler race/crash tests and Stage-H account-dispatch/approval tests.
- `apps/worker/tests/integration/test_stage_g5_real_runtime.py`
- `tests/e2e_support/` and `apps/web/e2e/` existing deterministic journeys.
- `pyproject.toml`, `uv.lock`, root `package.json`, `apps/web/package.json`, `Makefile`
- `scripts/check.py`, `scripts/e2e.py`, `scripts/linux_qualification.py`,
  `scripts/bootstrap.py`, `scripts/dev.py` and `scripts/clean_check.py`.

Read relevant implementation sections fully before editing; avoid dumping enormous
files into truncated outputs. Follow symbol references and focused slices.

## 4. W0 — Finish contract freeze before code

Reconcile ADR 0039 with the approved plan and inspected predecessor contracts.
Define exact typed interfaces, JSON transport schemas, transition table and
failure matrix. Document source/target states, transaction boundaries and errors.

Keep AgentPackage, AgentInstance, WorkflowExecution, Step, Run, Attempt,
Conversation, MemoryItem and Checkpoint distinct. Waiting is workflow state,
not a new Run status that bypasses the existing execution state machine.

Honor ADR 0039 limits:

- 32 default / 128 hard maximum steps.
- 16 default / 64 hard maximum active workflows per owner.
- Checkpoints: 64 KiB UTF-8, depth 16, 4096 nodes, JSON string keys, finite numbers.
- Wakeup signal: 8 KiB; inputs still obey ordinary Run bounds.
- Deadline: 24 hours default, seven days maximum.
- Model-call reservations: 256 default / 2048 maximum.
- Tool-call reservations: 256 default / 2048 maximum.
- Output-token reservations: 262144 default / 2097152 maximum.
- At most 128 committed checkpoints per workflow; define terminal retention and
  explicit owner cleanup/archive without silently removing execution audit.

Distinguish reserved capacity from actual usage. No unsupported claim of a hard
input-token budget or exactly-once remote effects. If an additional bound is
needed, record its rationale and compatibility effect in the decision.

W0 exit: a precise contract and mapping to existing seams; no unresolved silent
conflict with Stage-F memory authority, Stage-G pins or Stage-H approvals.

## 5. W1 — Domain, persistence and atomic checkpoints

Suggested new modules, subject to existing naming conventions:

- `packages/nervos-core/src/nervos_core/domain/workflows.py`
- `packages/nervos-core/src/nervos_core/application/workflows.py`
- `packages/nervos-core/src/nervos_core/infrastructure/database/workflows.py`
- The next local Alembic migration, expected `0024` if head is still `0023`.

Implement owner-scoped workflow, step, checkpoint and wakeup/decision evidence
records with typed application ports. Choose the smallest schema supporting the
approved behaviors; do not add a generic workflow framework or a second Job table.

Required persisted information:

- Owner, instance, submission identity/content digest and creation/update times.
- Workflow application/state schema version, current checkpoint revision,
  lifecycle, pause flag, deadline and wait reason/wakeup identity.
- Initial model, definition, package/environment/configuration identity.
- Step sequence, expected checkpoint revision and unique linked ordinary Run.
- Bounded checkpoint data, digest and successful step provenance.
- Cumulative limits/reservations and safe decision/signal provenance.

Use database constraints for unique owner submission identity, workflow/step
sequence, single Run linkage, legal statuses, nonnegative revisions/budgets,
foreign keys and useful bounded-scan indexes. Preserve UTC representation and
the repository's SQLite transaction behavior. No manual schema modification.

Pin each step's incoming checkpoint at acceptance. The package sees immutable
data and cannot supply owner, Run/Attempt authority, arbitrary workflow IDs,
lease tokens, credentials or database handles.

Extend the result path additively from package-host -> PackageExecutionAdapter ->
ChatOutcome -> ExecutionOutcome -> JobExecutionService -> fenced persistence.
Commit successful Run terminalization and checkpoint/progress atomically using
the same connection. Require active Attempt authority and expected revision.
Reject stale, concurrent, oversized, malformed or foreign writes. Validate bounded
results before terminalization so invalid package data cannot strand a Run.

No independent writable mid-step checkpoint API. One bounded step executes and
returns its checkpoint/directive; unsaved intermediate process memory is lost
on failure. Older non-workflow Runs retain exactly their existing behavior.

Update API metadata and Worker/Scheduler schema expectations together. Update
migration assertions intentionally; do not merely change strings to hide failures.
Hosted Marketplace migrations remain independent.

W1 exit: migration parity, owner isolation, JSON bounds, fenced atomicity and
restart persistence proved against isolated databases.

## 6. W2 — Continuations, waits and recovery

Workflow application directives support completion, next step and bounded waits
for time/signal/owner decision. Freeze validation and mutually exclusive fields.

Use `insert_run_and_job_on_connection` for initial and continuation Runs. Workflow
progress, reservation and unique step linkage must commit with accepted Run/Job
on one serialized transaction. Reconcile uncertain commit outcomes from durable
submission/step identity; never blindly insert another Run.

Integrate a bounded workflow tick into the existing Scheduler composition/loop.
Do not execute models/tools in the Scheduler, add a second queue, change Stage-E
cron/DST semantics or poll every workflow without indexes and bounded pagination.
Use fair pagination so one blocked workflow cannot hide all later due work.

Before continuation, verify owner, enabled instance, pinned runtime identity,
deadline, pause/cancel state, checkpoint revision and remaining budget. A changed
model/package/configuration causes needs_review rather than automatic repinning.
Each accepted step receives fresh ordinary context under existing memory policy;
the prior step's snapshot remains immutable.

Reserve the whole per-Run model/tool/output-token capacity before step acceptance.
Keep reservations across restart and failures; max_attempts is one for workflow
Jobs under ADR 0039. Avoid token retry accounting that silently resets across Runs.

Signals are authenticated owner operations with opaque idempotency identity,
canonical content digest, expected revision and wait identity. Identical replay
returns the prior result; changed content conflicts; stale/out-of-order deliveries
cannot advance the wrong wait. Tie-break cancel/wakeup races transactionally.

Project ordinary Run status truthfully. Terminal failed/cancelled/lost execution
does not become a new automatic step; surface needs_review and retain evidence.
Restart recovery projects durable outcomes and submits already-decided unique
continuations; it never repeats a model/tool call to reconstruct a checkpoint.

Pause stops future steps at boundaries. Resume clears that flag only on eligible
nonterminal state; it must not fabricate a signal/approval. Workflow cancellation
stops continuations and exposes cancellation of a current Run separately through
existing cancellation authority. Explain any remote request uncertainty.

W2 exit: bounded Scheduler ticks, no duplicate continuation, ordinary admission
and concurrency, restart-safe waits, correct pause/cancel and durable limits.

## 7. W3 — Owner decisions and external-effect recovery

Separate a durable decision about proposed application work from Stage-H's exact
live-Attempt action approval. Never reuse a terminated Attempt's approval.

Bind each proposed decision to owner, workflow/checkpoint revision, exact tool
identity/schema/source, canonical arguments digest, initial package/configuration
identity and expiry. Show the exact proposed action privately to the owner.
Reject edited, expired, cancelled, foreign or already consumed decisions.

On resumed execution, require current grants, connection ownership, account
status, token lifecycle, publisher trust and any existing H3 live approval again.
Decision approval must not grant a tool or reveal a credential.

Reuse ToolInvocation intent/status/receipt evidence. Define safe ambiguous-outcome
handling: provider success followed by persistence/process loss requires review,
or a connector-specific safe read with a documented reconciliation contract.
Never retry a write simply because a workflow checkpoint is older than its effect.
Keep provider idempotency claims specific to providers that actually support them.

The first connector is read-only. Sending and destructive operations remain
disabled; a proposed email response is data, not a permission to send it.

W3 exit: exact decision binding and rejection tests, live revocation tests and
fault-injection proof that uncertain writes are not replayed.

## 8. W4 — Additive SDK and qualified LangGraph adapter

Add `workflow-v1` host capability and immutable SDK WorkflowSnapshot/WorkflowResult
types with explicit state version and expected revision. Preserve SDK 0.1 legacy
packages and ordinary AgentResult behavior. A legacy result completes a workflow
after one step. Old hosts lacking workflow capability must refuse workflow steps
before package code runs, with a clear safe compatibility error.

Keep frame, aggregate-output, execution-time and JSON bounds enforced on both
host and Worker. Dataclass freezing alone is not validation or transport safety.

Implement a framework-neutral example first. Then one pinned LangGraph adapter
that executes one bounded graph node per ordinary Run, stores JSON application
state, and returns explicit NervOS continuation/wait results. Preserve node order
and state schema version through checkpoint round trips.

Before selecting versions or implementing framework/provider APIs, consult current
official LangGraph/LangChain/Google documentation using available documentation
tools. Pin the qualified versions and update lockfiles intentionally. Do not guess
versions or add unnecessary dependencies to core. Optional framework code belongs
behind the public SDK boundary, not inside domain or persistence modules.

Use NervOS ModelPort and ToolPort for all model/tool operations. Explicitly qualify
supported message roles and tool-call IDs. If current ports cannot represent a
framework feature, implement a reviewed additive contract or mark it unsupported;
do not silently flatten/lose identities while advertising complete support.

Publish a compatibility matrix: exact versions, state types, messages, tool calls,
wait/interrupt mapping, model/tool mediation and unsupported features. Native
framework pickle/checkpointers, arbitrary serialized interrupts, arbitrary subgraphs
and universal SDK compatibility are not part of this qualification.

W4 exit: legacy compatibility, host feature negotiation, narrow adapter and
checkpoint/message/tool/wait contract tests through actual package IPC.

## 9. W5 — Gmail read-only connector and usable dashboard

Use Gmail as the first mail provider under ADR 0039. Verify the official current
OAuth/Gmail contracts. Build list/search/read only, with least necessary scopes.
No send, draft mutation, label mutation or delete capability in this first release.

Use the existing operator-controlled account OAuth/PKCE and encrypted secret
manager with server-side token custody. Define documented development settings
with fake placeholders, redirect setup and exact required scopes. Treat real
provider authorization/consent as manual acceptance, not a mocked live success.

Host connector transport outside untrusted packages. Route it through existing
tool mediation, account broker, live authority and invocation audit. A connected
account or installed package grants nothing automatically. Make account selection,
tool discovery/binding and per-instance grant/revoke practical in existing UI.

Bound query sizes, message counts, response bytes, pagination, timeout and retry.
Use fixed provider endpoints and safe encoding. Do not fetch arbitrary URLs from
message content, logs or package configuration. Avoid logging bearer headers,
token endpoint bodies or private email content. Provider errors use safe normalized
codes; distinguish expiration/revocation/outage from an empty mailbox.

Add typed API resources and thin authenticated routes for workflow creation,
list/detail, step/checkpoint evidence, signals/decisions and lifecycle controls.
Use current user ownership, Origin validation, input Pydantic schemas, bounded
keyset pages, expected-revision CAS and safe not-found/conflict/error mappings.
No direct database or execution calls in React or route business logic.

Suggested UI additions:

- `apps/web/src/api/workflows.ts` and associated typed query keys/hooks.
- `apps/web/src/pages/WorkflowsPage.tsx`.
- `apps/web/src/pages/WorkflowDetailPage.tsx`.
- Home/navigation links and an AgentInstance action to start a workflow.

Show lifecycle, current step/Run, pause flag, wait reason, next wakeup/deadline,
linked Runs and timelines, checkpoint revision/safe summary, proposed decisions,
reserved versus reported budgets, and concrete recovery guidance. Private full
state needs explicit owner inspection, not a public health/audit projection.

Provide empty/loading/error states, keyboard access, labels, visible focus,
readable contrast and narrow-screen layouts. Poll only active resources, handle
stale revision conflicts, disable duplicate submissions and clear private caches
on logout. Explain that waiting differs from a Run executing or waiting for a Worker.
Display Linux package capability truth from executing Workers, not API-host guesses.

W5 exit: API ownership/security tests, deterministic connector tests and accessible
browser journeys exercising real backend contracts.

## 10. W6 — Demonstrations, integration and documentation

Build two versioned `.nervos` example applications using public interfaces:

1. Research: collect bounded sources through explicitly granted tools, checkpoint
   collected evidence, continue to a model-generated brief with source provenance.
2. Mail triage: list/read allowed messages, checkpoint classification/progress,
   prepare proposed responses and wait for an owner decision. Do not send mail.

Keep examples reproducible, offline-buildable with verified wheel closure and
safe fake credentials. Do not modify the separate user research project outside
this repository without an explicit request.

Exercise real package-host IPC and production pre-exec containment. A fake adapter
or monkeypatched sandbox can test application logic but cannot prove integrated
package execution. Use deterministic model and mail providers to avoid real data,
charges and external flakiness in automated acceptance.

Document full startup: bootstrap, local migration, API, Worker, Scheduler, web,
operator OAuth setup, package build/install, instance config, account/tool grants,
workflow creation, waits/decisions, restart and cleanup. Verify commands rather
than inventing unavailable subcommands. Current Scheduler command is
`uv run python -m nervos_scheduler`; scripts/dev.py does not yet accept scheduler.

Separate Windows control-plane/trusted-agent support from qualified Linux package
execution. Keep Windows/macOS refusal. Native Linux, WSL/Docker kernel evidence,
fake provider and real-provider checks must be named accurately.

Update runtime, SDK/package, configuration, database, permission and workflow docs
with actual delivered behavior. Record acceptance evidence and remaining deployment
limits. Reconcile status wording only after tests pass; do not declare hosted
Marketplace production or Stage-J completion as a side effect of this work.

## 11. Required test matrix

Use isolated temporary databases, deterministic clocks/providers and explicit
failure injection. Start with focused behavior tests; finish with full regression.

### Domain and contracts

- Legal/illegal transitions, pause versus cancel and terminal-state immutability.
- Empty and maximum state, byte/depth/node bounds, Unicode, malformed JSON,
  non-string keys, NaN/infinity, unsupported state versions and executable state.
- Directive exclusivity, deadline/timezone validation and cumulative reservations.
- SDK deep immutability and host capability/frame compatibility.

### Database and migrations

- Empty database -> head; previous head -> new head with preserved representative
  data; model/migration parity; constraints, indexes and foreign keys.
- Document safe downgrade behavior, including explicit refusal to destroy evidence.
- Wrong Worker/Scheduler schema refuses before mutation.
- Foreign owner/instance/checkpoint/signal/decision cannot be probed or changed.
- Revision CAS and stale Attempt fence reject writes without partial terminalization.
- Success and checkpoint/progress commit together, including injected failures.

### Continuations and recovery

- Same creation identity/same content replays; changed content conflicts.
- Two Schedulers racing submit one next Run and one step.
- Crash before/after Run commit and checkpoint commit; uncertain commit reconciles.
- Duplicate, late, out-of-order signals; wakeup versus pause/cancel races.
- Worker restart/lost lease and Scheduler restart do not replay ambiguous effects.
- Waiting uses no execution capacity; concurrent workflows obey ordinary limits.
- Backpressure does not lose intent or starve later due workflows.
- Deadline, step, model/tool/output-token limits survive restart and stop new work.
- Changed configuration/package/model refuses continuation; fresh memory selection
  affects only new snapshots, not prior checkpoints/context.

### Decisions, accounts and tools

- Exact arguments/tool/package/revision/owner/expiry binding and tamper rejection.
- Workflow decision cannot reuse or substitute for H3 live-Attempt authority.
- Account/grant/token/trust revocation while waiting blocks later dispatch.
- Remote success before local failure becomes review-required, never blind retry.
- Fake OAuth authorization, refresh, expiry, revoke and secret-key rotation.
- Gmail endpoint/scope/query/response/timeout/pagination/error contracts.
- Package never obtains raw credentials or direct filesystem/network access.
- List/read only: no callable send/delete path or automatic grants.

### SDK, framework and integrated runtime

- Legacy packages and non-workflow Runs retain behavior.
- Old host rejects workflow capability before importing package code.
- Model messages/tool identity/wait results survive the actual transport.
- LangGraph one-node-per-Run state round trip and version mismatch handling.
- Research and mail workflows complete/wait/resume across process restarts.
- Production launcher isolation probes and Windows/macOS fail-closed regressions.

### API and UI

- Missing auth, invalid Origin, IDOR, pagination, duplicate POST, revision conflict.
- Workflow create/detail/wait/decision/pause/resume/cancel/review journey.
- Private cache cleared on logout; no checkpoint/token leakage into public health.
- Accessible labels/focus, keyboard operation, contrast and mobile layout.
- Worker absent, Scheduler absent, storage unavailable and provider outage show
  truthful recoverable states rather than false success or permanent spinners.

## 12. Verification commands

Inspect commands first and use repository tooling. Run from the repository root.
Record exit codes, counts/skips and reasons; do not claim tests were run from an
older report. The prior Stage-H record (3354 Python passes/5 skips, 163 frontend
passes, 12 Linux properties, 4 Linux browser journeys) is historical baseline only.

```powershell
git status --short
git diff --stat
uv run python scripts/check.py lint
uv run python scripts/check.py typecheck
uv run python scripts/check.py test
uv run python scripts/check.py security
pnpm build
uv run python scripts/check.py e2e
```

Run focused new tests and affected predecessor tests before the full gates.
`scripts/check.py check` aggregates lint/typecheck/test/security, not build/E2E.
Default pytest excludes hosted Marketplace integration; it is not proof of those
external services. Run `scripts/check.py marketplace-integration` against disposable
services if impacted/required for full acceptance, and report exclusions honestly.

Run on a qualified Linux kernel with bubblewrap and dependencies:

```text
uv run python scripts/linux_qualification.py
```

Extend integrated acceptance to exercise workflow packages through production
containment. `--probes-only` is insufficient. Exit code 2 denotes a prerequisite
skip, not successful qualification. Do not disable namespaces/network denial to
get a green package test. Docker/WSL may supply Linux evidence, not native-host proof.

Migrations must be tested on isolated databases through the existing migration
fixtures. Do not run test downgrade/upgrade against the developer database.
Use clean_check.py only after understanding its copy/worktree behavior; do not
let a tracked-only clean check omit the required untracked predecessor files.

Fix failures without deleting tests, broad ignores, weakened assertions or security
bypasses. If an external prerequisite is unavailable, continue independent work,
record the exact unverified gate and do not mark that milestone accepted.

## 13. Explicit restrictions

- No second queue, alternate lease engine or long-running process per idle agent.
- No raw account tokens, credentials or database access in agent packages/frameworks.
- No direct SDK/provider HTTP bypass; no sandbox downgrade for demonstrations.
- No automatic cross-owner/cross-agent memory, delegation or Stage-J implementation.
- No checkpoints silently promoted into persistent facts or system instructions.
- No changing grants implicitly when an owner approves a workflow decision.
- No replay of uncertain writes, reuse of dead-Attempt approvals or exactly-once claims.
- No automatic package/config migration of an existing workflow on resume.
- No arbitrary pickle, framework-native DB state or broad SDK compatibility claims.
- No real mail writes/deletion; no unrelated whole-project UI rewrite.
- No Redis/Kafka/Kubernetes or replacement of local SQLite/hosted S3/PostgreSQL.
- No destructive cleanup, history rewrite, Git finalization or external publication.
- No completion claim based solely on focused/fake tests or historical evidence.

## 14. Final deliverables and report

Required deliverables:

1. Reviewed precise workflow contracts and ADR 0039 alignment.
2. Local migration, typed core services and fenced checkpoint/continuation engine.
3. Scheduler integration and durable lifecycle/decision/recovery controls.
4. Additive SDK/host contracts and the pinned qualified LangGraph adapter.
5. Read-only Gmail connector using existing security/account/tool boundaries.
6. Usable owner-scoped Workflows UI integrated with agents/accounts/grants.
7. Two reproducible `.nervos` demos and a tested startup/demo runbook.
8. Tests, compatibility matrix, acceptance report and accurate implementation status.

Final report must state:

- W0–W6 status separately: implemented, verified, externally pending or blocked.
- What changed, why, important files and actual migration head.
- Exact check results and platform/provider distinctions.
- Demonstrated workflow behavior, failure/recovery behavior and security boundaries.
- Remaining limits, unverified checks and real-provider setup prerequisites.
- Copyable startup and demo commands or a link to the verified runbook.
- Whether anything still prevents declaring W0–W6 complete; never hide it.

The final observable result is that an owner can install a compatible application,
create an instance, start a workflow, inspect persisted progress, let it wait
without occupying a Worker, restart services, and continue through ordinary Runs
with current permissions and bounded cumulative work. Multiple workflows share
the runtime safely. An uncertain step stops for review instead of replaying an
external effect. This must be demonstrated, not merely described in documentation.

---
