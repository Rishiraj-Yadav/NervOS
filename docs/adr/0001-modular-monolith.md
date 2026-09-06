# ADR 0001 — Start as a Modular Monolith

Status: Accepted

## Context

NervOS has many conceptual components, but starting them as independent network services would add deployment, networking, authentication, observability, and local-development complexity before the product runtime is proven.

## Decision

Build the initial self-hosted NervOS node as a modular monolith with explicit internal boundaries and only a small number of processes when needed (for example API and workers later).

## Consequences

Positive: simpler installation, debugging, backups, and old-PC/self-hosted operation.

Tradeoff: internal boundaries must be maintained deliberately. Split services only when measured requirements justify it.
