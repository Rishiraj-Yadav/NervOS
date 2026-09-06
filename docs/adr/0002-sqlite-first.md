# ADR 0002 — SQLite First

Status: Accepted

## Context

The early deployment target is one self-hosted NervOS node. Requiring PostgreSQL/Redis would make setup heavier.

## Decision

Use SQLite initially through SQLAlchemy/Alembic. Use WAL mode when concurrent worker access later requires it.

## Consequences

Very simple install/backup and no external DB server. PostgreSQL may be supported later for larger shared deployments without changing public SDK contracts.
