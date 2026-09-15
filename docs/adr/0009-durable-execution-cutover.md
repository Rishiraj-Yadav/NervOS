# ADR 0009 — Durable Execution Cutover

Status: Accepted for C2 implementation

## Context

Stage B proved the trusted `nervos.chat@1` path by awaiting one bounded model call inside the API process. Stage C1 added durable `jobs`, `job_attempts`, and `run_events`, but left them dormant. C2 activates the execution plane without adding new schema.

## Decision

Run creation is split into control-plane submission and Worker execution.

- The API accepts a Run by committing `Run + Job + initial Run Events` and returns `202 Accepted`.
- The API holds no provider credential and constructs no provider SDK client.
- A separate Worker process claims queued Jobs using SQLite `BEGIN IMMEDIATE`, creates an Attempt, commits an execution-start boundary, runs the existing trusted Chat behavior, and terminalizes Attempt, Job, and Run.
- Workers claim only Jobs whose `model_provider` is in their configured provider set. Missing credential capability is a queue state, not a Job failure.
- Every owner write is fenced on Worker ownership, claim token, active state, and an unexpired lease.
- C2 records retry disposition evidence but does not retry execution or recover expired leases.

## Consequences

Accepted work survives API request completion and API process restarts as queued durable state. Execution can be run in a separate process with its own provider credentials, reducing control-plane credential exposure.

C2 intentionally leaves several operational gaps for later milestones: crash recovery, retry scheduling, cancellation, fairness, worker registry/health, event streaming, and per-Agent/per-provider concurrency. A Job already `claimed` or `running` when a Worker dies remains stranded until reconciliation is implemented.

ADR 0007's awaited API-process proof-runner boundary is superseded for production execution; the trusted behavior and provider-neutral model port remain in use behind the Worker.
