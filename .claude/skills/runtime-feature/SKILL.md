---
name: runtime-feature
description: Modify the NervOS execution runtime, jobs, workers, run state machine, retries, concurrency, scheduling integration, or run events while preserving the job-driven architecture. Intended for Stage B and later.
---

# NervOS Runtime Feature

Read architecture rules, `docs/runtime.md`, `docs/permissions.md`, and relevant ADRs.

## Protect invariants

- AgentInstance != Run
- Session != Run
- triggers create Jobs
- Jobs are persisted before execution once queue exists
- workers execute jobs
- active agents do not require permanent processes
- state transitions are explicit
- side effects remain permission-gated

## For state changes document

- allowed source states
- target state
- atomic persistence needs
- retry/cancel behavior
- crash recovery behavior
- user-visible events

## Concurrency

Avoid double execution. Once persistent workers exist, use claims/leases/transactions. Default AgentInstance concurrency remains one unless deliberately changed.

Add tests for illegal transitions, retries, crash/lease behavior, and cancellation where relevant.
