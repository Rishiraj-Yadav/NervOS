# ADR 0004 — Job-Driven Agent Runtime

Status: Accepted as target architecture

## Context

Most installed agents are idle most of the time. One permanent thread/process per installed agent wastes resources and complicates recovery.

## Decision

Agent execution is trigger/job driven:

`Trigger -> persistent Job -> Queue -> Worker -> Run`

An active AgentInstance is persisted state, not a permanently executing process.

## Consequences

Many installed agents can coexist cheaply, concurrency is centrally controlled, and user/schedule/event triggers share one execution path.
