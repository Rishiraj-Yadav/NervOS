# ADR 0039 — Durable autonomous workflows

Status: Accepted for implementation by the owner, 2026-10-04. Qualification pending.

## Boundary

A WorkflowExecution owns ordered steps, each linked to an ordinary Run/Job.
The canonical insertion, admission, fairness, Worker leases and tool broker remain
authoritative. No additional execution queue or permanent agent process exists.
Only the Scheduler materializes due continuations. Existing schedules/events can
start ordinary runs; workflow wakeups add bounded durable signals to that same
Scheduler process. An authenticated owner signal is not a public webhook.

Checkpoints are private application data, never instructions, permissions or
memory. Every continuation builds a new ordinary context snapshot, including
currently eligible memory. The separately delivered checkpoint is immutable for
that Run. No Stage-F retrieval bounds or memory promotion authority changes.

## State and atomicity

`pending -> runnable -> running -> waiting | runnable | succeeded`. Terminal
Run failure or cancellation projects to `needs_review`; malformed results and
exhausted limits fail the workflow. Owner cancellation is terminal. Pause is a
durable boundary flag: an active Run finishes, but no next Run is accepted.

Success, checkpoint revision N+1, directive and workflow progress commit on the
same connection after the existing Attempt/Job/Run success fence. A stale Attempt
cannot checkpoint. There are no mid-step checkpoint writes. A step is one bounded
application node. Expected revision must equal the immutable step revision.
Each (workflow, step number) and Run link is unique. Submission identity is
owner-scoped, content-digested and replayable after uncertain commits.

Each continuation verifies initial package/config/model identity. A changed
binding/configuration causes needs_review; owners start a new workflow explicitly.
Tool grants, connected accounts and trust are checked live by the existing runtime.
Failed/expired Attempts never resurrect; all workflow Jobs have one Attempt.

## Bounds

Default/hard bounds: 32/128 steps, 16/64 active workflows per owner; checkpoint
64 KiB UTF-8, depth 16, 4096 JSON nodes, string keys, finite numeric scalars only;
signal 8 KiB; step input existing Run limits; retained checkpoints at most 128.
Deadline defaults to 24 hours and cannot exceed seven days. Per-step execution
timeouts retain their existing meaning; the workflow deadline forbids future
dispatches rather than promising immediate interruption of a remote request.

Model/tool calls and output-token capacity are conservatively reserved from each
Run's limits before acceptance. Reservations survive restart and are not refunded.
Defaults: 256 model calls, 256 tool calls, 262144 output tokens; hard maxima:
2048, 2048 and 2097152. Exact input token accounting is unavailable for some
providers: input/context byte bounds remain authoritative, reported token usage
is observational and is not advertised as a hard total input-token cap.

## Decisions and external effects

A durable owner decision authorizes a proposed next application step only. It
does **not** license tool dispatch or replace ADR 0037's same-live-Attempt approval.
Decision identity binds workflow revision, exact tool identity/arguments digest,
initial package identity and expiry. Changing it invalidates the decision. Each
dispatch still requires current grants, account/trust and any live H3 approval.

Existing ToolInvocation records are the external action intent/receipt ledger.
Success followed by Worker loss never implies safe replay. An uncertain outcome
requires review; reconciliation may use only an explicitly safe read. No generic
exactly-once remote-effect guarantee or automatic retry of writes is provided.
The initial Gmail connector exposes only list/search/read, never send/delete.

## SDK and compatibility

Add `workflow-v1` host capability, immutable WorkflowSnapshot and WorkflowResult
to SDK 0.1. Old packages without a directive complete after one step. The Worker
refuses workflow execution with an old host lacking that capability. JSON state
version is explicit. Framework-native pickle/database checkpointers are forbidden.
The first LangGraph adapter executes one node per ordinary Run using SDK model
and tool ports, JSON checkpoints and explicit wait results; arbitrary frameworks,
subgraphs and native serialized interrupts are not claimed compatible.

## Failure matrix

| Boundary | Recovery |
|---|---|
| Before Run submission commit | Retry same owner submission identity |
| After Run/step commit, before response | Read existing identity; no second Run |
| During leased execution | Existing Run recovery; no workflow replay |
| Provider effect before loss | needs_review; inspect receipts, no blind retry |
| Success/checkpoint commit before wakeup | Scheduler submits unique next step |
| Duplicate/outdated signal | Replay identical delivery; reject stale revision |
| Pause/cancel racing wakeup | Serialized write transaction decides; no later step |
| Configuration, trust or grant changes | New step/dispatch refuses stale authority |

Hosted Marketplace schema and Stage-J sharing/delegation remain independent.
