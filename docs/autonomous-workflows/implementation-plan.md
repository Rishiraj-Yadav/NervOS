# Autonomous workflows — proposed implementation plan

Status: W0–W6 implementation authorized by the owner on 2026-10-04. Acceptance is
pending; approval is not evidence of delivery. ADR 0039 freezes the first contract.

## Baseline and evidence

The supplied Stage-H completion report and `docs/implementation-status.md` record
3,354 Python tests passed with five skips, 163 frontend tests passed, twelve Linux
isolation properties passed and four supervised Linux browser journeys passed.
These are recorded results, not tests rerun for this planning task. Linux evidence
uses the Docker Desktop WSL2 kernel; native Linux acceptance remains open. Windows
and macOS package execution continue to refuse. Hosted Marketplace production/OIDC
acceptance remains separate Stage-I work.

The reviewed SDK provides model/tool ports, bounded context, selected memory and
memory proposals. It does not provide durable workflow checkpoints. ADR 0037 permits
action approval only within the same live Attempt. A terminated Attempt cannot be
resumed with an old approval. These boundaries govern the proposed work.

## Intended outcome

NervOS hosts persistent, event-driven agent applications. An application retains
progress while idle and advances through bounded ordinary Runs. It can serve email,
research, document review and other domains without keeping every agent process alive.

The recommended next feature is a durable workflow foundation, followed by one
qualified framework adapter and one account connector. Multi-agent handoff and
workspace/shared memory remain governed by Stage J. This proposal is a runtime
extension and prerequisite for collaboration; it does not silently rename Stage J
or assign its implementation milestones.

## Milestones in implementation order

### W0 — Architecture and protocol freeze

Prepare reviewed ADRs for workflow identity/lifecycle, checkpoints, continuation
submission, side-effect recovery, waits/approval, SDK compatibility and budgets.

Keep these distinct: AgentInstance, WorkflowExecution, Step, Run, Attempt,
Conversation, MemoryItem and Checkpoint. A WorkflowExecution references ordinary
Runs; the existing Job queue remains the sole execution queue.

Freeze bounded JSON checkpoint schemas, versions, serialization, retention,
ownership, package/config pinning, deadlines, deduplication and failure semantics.
Reject arbitrary pickle, executable state and package-supplied owner/lease authority.
Set explicit defaults for checkpoint/event size, number of steps, concurrent
workflows, model/tool calls, token usage and wall-time budgets. Budgets survive
continuations and restarts; a new Run cannot reset them.

Acceptance: reviewed interfaces, lifecycle tables, failure matrix and predecessor
compatibility mapping. A successor decision must explicitly address durable waits
where they extend ADR 0037; implementation cannot reinterpret existing approvals.

### W1 — Durable state and checkpoints

Implement owner-scoped WorkflowExecution and versioned checkpoints with Alembic
migrations, repositories and application services. Persist step input/result,
configuration/package identity and provenance using bounded typed records.

Checkpoint writes require the active Run/Attempt fence and expected revision.
Reject stale, concurrent or foreign writes. Keep successful step completion and
checkpoint advancement transactionally consistent. Migration and Worker/Scheduler
guards move together. Hosted Marketplace schema remains independent.

Application state belongs here; preferences/facts continue through scoped memory.
Memory suggestions remain policy controlled and are eligible for future context
snapshots. Context is frozen per Run; continuing a workflow creates a new snapshot
under an explicitly reviewed context contract.

Acceptance: persistence across restart, owner isolation, version conflict handling,
size/retention bounds and migration parity.

### W2 — Continuations, waits and recovery

Submit continuation Runs through the existing submission primitive. Persist the
decision and deduplication key so crashes cannot enqueue duplicate next steps.
Reuse Stage-E schedules/events for wakeups. Waiting consumes no Worker execution
slot and requires no long-lived package process.

Provide pending, runnable, running, waiting, succeeded, failed, cancelled and
needs-review workflow states, separate from existing Run statuses. Define timeout,
cancel and restart behavior for every transition. Resume a permitted next step;
never resurrect a terminated Attempt or replay an ambiguous external action.

Acceptance: restart before/after checkpoint and next-Run submission, duplicate and
out-of-order events, cancellation/wakeup races, fairness and cumulative budgets.

### W3 — Safe external actions and durable owner decisions

Preserve Stage-D tool grants, invocation audit and Stage-H broker checks. Record
action intent and provider receipt without claiming exactly-once remote effects.
Use provider-supported idempotency only where the connector contract proves it.
After an uncertain remote outcome, reconcile through a safe provider read or mark
the workflow needs-review; do not retry blindly.

Separate a durable owner decision about proposed work from H3's live dispatch
approval. Bind any new decision to owner, exact arguments, tool identity, package
identity, revision and expiry. A new Run must recheck live grants/account/trust and
obtain the exact dispatch authority required by the reviewed successor contract.
Editing an action invalidates prior authorization.

Acceptance: zero/one permitted dispatch, provider-success-before-crash recovery,
token/grant revocation while waiting, changed action refusal and cancelled decision.

### W4 — Public SDK and one qualified framework adapter

Add versioned SDK workflow ports and an additive package-host protocol feature.
Existing packages remain compatible. Packages never receive DB handles, connector
tokens or raw queue authority. Store checkpoints through the host/Worker boundary.

Qualify a minimal framework-neutral workflow first, then one pinned LangGraph
adapter with any necessary LangChain model/tool bridges. Explicitly test supported
message roles, tool-call identities, interrupts, checkpoint round trips and state
versions. Route models and tools through NervOS ports; direct framework HTTP
clients cannot bypass the sandbox, grants or credential broker.

Acceptance: publish a narrow compatibility matrix with pinned versions and supported
features. Do not advertise universal SDK compatibility or loading arbitrary
framework-native serialized state.

### W5 — Account connector and operator UI

Build the first connector for one mail provider, starting with list/search/read.
Use operator-controlled OAuth, least scopes, bounded transport and token custody.
The connector is controlled external access; an agent package supplies application
logic. Installing a package or connecting an account never grants every agent access.

Add a Workflows page and detail timeline: current step, wait reason, next wakeup,
safe checkpoint summary, linked Runs, actions, consumed budgets, pause/resume/cancel
and recovery requiring attention. Make account selection and per-instance tool
grants usable through existing Security/Connections/agent surfaces.

Support restart-safe pause at step boundaries. Distinguish stopping future steps
from cancelling a currently executing Run. Show safe recovery guidance and redact
private account data in public audit/status copy.

Acceptance: fake-provider OAuth/refresh/revoke and connector contracts, accessible
browser journeys, owner isolation and truthful status during outages.

### W6 — Two-domain demo and integrated acceptance

Demonstrate both a research workflow and a read-first mail-triage workflow on the
same runtime. Research collects mediated source results and produces a persisted
brief. Mail triage reads allowed messages, classifies them and prepares a proposed
response. Enable sending only after the exact-action authorization and ambiguous
outcome tests pass; destructive mail operations stay outside this first connector.

Exercise concurrent workflows, delayed events, restart mid-step, provider outages,
revocation, approval delay, cancellation, duplicate delivery and budget exhaustion.
Verify checkpoint recovery does not duplicate remote effects. Qualify the production
package launcher throughout, plus full regression/static/migration/security/browser
checks. Record native versus WSL evidence honestly and provide a startup/demo runbook.

## Module responsibilities

| Module | Responsibility |
|---|---|
| nervos-core | Workflow domain, state, fenced persistence, continuation/decision services |
| Worker | Bounded step execution, SDK mediation, checkpoint/result finalization |
| Scheduler | Existing due-wakeup mechanisms; no second queue |
| API | Authenticated owner controls and safe projections |
| Web | Workflow lifecycle, timelines, budgets and recovery controls |
| SDK/package host | Additive workflow port and bounded transport contracts |
| Connector transport | Approved account access, scopes, receipts and provider recovery reads |

## Required compatibility and limits

- Preserve modular monolith dependency direction and Run/Job/Attempt authority.
- Keep private memory, conversations and workflow state separate.
- Recheck grants, accounts, trust and cancellation at every dispatch boundary.
- Keep package runtime identity pinned; migration to a new workflow/package version
  is an explicit owner operation, never automatic during resume.
- Use deterministic providers and isolated databases for automated acceptance.
- Extend schema only through Alembic; preserve existing data and audit evidence.
- Preserve Windows refusal; a supported Linux deployment is required for these
  package demos. Document that prerequisite before claiming demo readiness.

## Work after the foundation

Freeze Stage J for explicit inter-agent task/handoff permissions, delegation limits,
orchestrator budgets and selected shared-workspace memory. Build a bounded handoff
between the qualified research and mail workflows as its demonstration. Never merge
all agent memories or let delegated work inherit permissions automatically.

Native Linux qualification and Stage-I production acceptance can proceed as separate
operational workstreams. They are prerequisites for the corresponding deployment
claims, not evidence already satisfied by this feature plan.
