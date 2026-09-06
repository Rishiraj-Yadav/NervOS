# ADR 0005 — Scoped Agent Memory

Status: Accepted as target architecture

## Context

Automatically sharing every remembered fact across all agents causes privacy, relevance, and coupling problems.

## Decision

Use explicit memory scopes: session, agent-private, user profile, and explicitly shared workspace. Agent-private memory is not automatically exposed to unrelated agents. Collaboration uses explicit handoffs/shared workspaces.

## Consequences

Stronger privacy boundaries, lower context pollution, and clearer access/deletion semantics at the cost of more explicit collaboration design.
