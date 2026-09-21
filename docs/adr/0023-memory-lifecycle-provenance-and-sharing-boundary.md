# ADR 0023 — Memory Lifecycle, Provenance, and Sharing Boundary

Status: Accepted

## Context

Persistent scoped memory needs durable provenance so users can inspect where a retained fact came from, controlled editing so changes are explainable and reversible at the application level, and deletion that removes facts from every active projection without pretending to erase historical immutable execution records, context snapshots, or backups. Cross-owner and cross-agent sharing is a later Stage-J concern and must not be inferred into Stage F.

## Decision

### Provenance

Durable memory scopes in V1 are `AGENT` (one owner and AgentInstance) and `USER` (owner-wide approved profile). There is no separate Conversation `MemoryItem` in V1; Conversation context is Messages + deterministic compaction. Every memory item records its owner, scope, its Agent scope ID where applicable, source type, and, where applicable, source message or Run. A USER/AGENT marker distinguishes user-authored from inferred content. Each edit creates a new version that supersedes the prior version and records supersession time. Provenance is owner/Agent scoped and never exposed across ownership without explicit authorization.

### Write authority

Permanent AGENT and USER memory writes require explicit user action or an explicitly user-approved promotion path. There is no automatic permanent-memory extraction in Stage-F V1; arbitrary model, tool, external, or summary text is never silently promoted into memory. Deletion, edit, and supersession are application lifecycle operations subject to the same explicit authority as creation.

### Active vs current

Active reads, retrieval, full-text/search projections, summaries, and context selection include only current non-deleted versions. Superseded and deleted versions are excluded from active retrieval even when retained for audit or user review.

### Editing and deletion

Edits use version/supersession, not in-place mutation. Deletion of an item supersedes and marks it deleted. Deleting, superseding, or editing invalidates relevant search projections, compaction selection, and context candidates. A concurrent edit against a stale version is rejected with a safe conflict; the application never silently last-writer-wins for defining memory content.

### Historical Runs, snapshots, and backups

Immutable historical Run records, the durable `RunContextSnapshot` (F2+), and audit records may retain the content a Run actually used. A snapshot stores the actual rendered context text for execution, which may include sensitive user-provided content; it never has secret-store credentials injected into it merely as context. F0 guarantees active-data deletion and invalidation, not cryptographic erasure from historical Run records, snapshots, or backups. NervOS does not implement a backup system in Stage F. Conversation archive/delete does not casually cascade-delete Runs, Jobs, Attempts, or audit events.

### Compaction derivation

Conversation compaction is deterministic, non-model derived data: versioned, provenanced by source sequence range, bounded, and rebuildable. Its invalidation follows the same active/current rule as memory. Because it is derived and rebuildable, it carries no independent authority and may be regenerated from committed messages.

### Sharing boundary

All memory is private by default. A future `WORKSPACE` scope requires explicit membership and Agent authorization and belongs to Stage J collaboration. Shared workspace memory is never inferred by merging all Agent or owner memory automatically. Stage F implementations add no cross-owner read or write path and no collaboration surface. If Stage-G package updates must touch package definition data, they must not bundle or delete personal Conversation or memory data.

### Recovery and ordering

Lifecycle operations are database transactions and re-run deterministically. Compaction refresh races use version/source-range checks with exactly one current winner, and compaction is regenerable from committed messages. Conversation deletion during an active Run does not cancel the execution; it removes conversation state from later context retrieval per policy.

## Consequences

This gives inspectable, versioned, deletable scoped memory with a clear active-versus-current rule and safe enforcement of cross-owner separation. It adds version/supersession and invalidation complexity and requires discipline to keep search projections rebuildable and non-authoritative. The story for hard/backup erasure and workspace collaboration is deliberately deferred; users should expect active-data deletion rather than cryptographic guarantees.
