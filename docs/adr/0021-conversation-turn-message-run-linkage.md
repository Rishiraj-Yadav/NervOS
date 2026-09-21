# ADR 0021 — Conversation, Turn, Message, and Run Linkage

Status: Accepted

## Context

NervOS currently distinguishes authentication sessions from Runs. A Run is an immutable execution snapshot with one ordinary Job and one or more Attempts; Stage E creates automated Runs without a conversation. The current model request has one user text plus tool-loop turns, but no durable conversational message ledger. Future conversational requests need durable ordering, retries, ownership, assistant-result semantics, and execute/recover-from-context safety without introducing a second execution engine.

## Decision

### Terminology and ownership

`auth_session` means the server-side login identity. `Conversation` means a durable interaction between exactly one owner and one `AgentInstance`; it is not an auth session, an AgentSession, or a Run. A conversation is owned by `(owner_user_id, agent_instance_id)`. A different owner or AgentInstance cannot read it absent a future explicit sharing authorization.

### Turn and message model

```text
Conversation → ordered Turn → one USER message → one-or-more ordinary Run links
                                            └→ at most one authoritative ASSISTANT message
```

A USER message is persisted exactly once. A Turn is the conversational submission identity, execution lifecycle, and retry boundary; a Run is one execution attempt. Durable roles are `USER` and `ASSISTANT`. Tool calls and results remain Stage-D `ToolInvocation` audit data and are never copied into the ordinary message ledger as trusted roles.

### History ordering (frozen)

Message order is derived from `turn.sequence` plus a fixed role order (`USER = 0`, `ASSISTANT = 1`). There is no independently allocated conversation-wide message sequence. Because a Turn has exactly one USER and at most one ASSISTANT, a recovered assistant message for Turn N always appears after that Turn's USER message and before Turn N+1. Retained invariants: one USER per Turn, at most one ASSISTANT per Turn, one authoritative successful Run per Turn.

### Run status and Turn finalization are distinct

Run execution status and Conversation Turn finalization are separate steps; a Run may reach `SUCCEEDED` before the Conversation projection is finalized. Finalization is one short idempotent database transaction:

```text
Run reaches SUCCEEDED
  ↓ (one short DB transaction)
  CAS/select authoritative_run_id
+ insert exactly one ASSISTANT message
+ transition Turn to SUCCEEDED
  ↓
commit
```

The first Run to transition the Turn to `SUCCEEDED` under this guarded (CAS) transition is the single **authoritative successful Run** per Turn; a later successful Run cannot become a second authoritative projection. Until the finalizer transaction succeeds, the Turn remains **active/non-finalized for Conversation sequencing**, so another Turn cannot be accepted yet. If the process crashes after Run success but before finalization, recovery re-runs **only the idempotent finalizer** and **never re-runs the model**; finalization projects from the already-durable authoritative Run output.

### Turn state machine and manual retry

A Turn has `PENDING`/`RUNNING` and terminal `SUCCEEDED`, `FAILED`, `CANCELLED`, `AMBIGUOUS`. Retry rules are frozen:

- The **latest** `FAILED` or `CANCELLED` Turn may be manually retried (new Run link, same USER message).
- A `SUCCEEDED` Turn cannot be manually retried.
- An `AMBIGUOUS` Turn gets no ordinary one-click same-turn retry; it is surfaced as ambiguous and the user sends a new message to continue.
- A non-latest, older Turn cannot be manually retried once a later Turn exists.

Automatic Stage-C retry remains the existing Attempt/Job behavior and does not create another message or conversational Run link unless the application explicitly starts a new manual Run. Stage F adds no retry or lease engine.

### Ordering, concurrency, and submission identity

`client_message_id` is the durable domain submission identity: an owner+Conversation-scoped opaque value stored on the Turn with a canonical content digest. Same ID plus same content replays the existing Turn/result; same ID plus different content is a conflict; a different ID creates a new Turn. Validation follows the existing repository opaque-idempotency convention: a non-empty string of at most 128 characters containing no NUL byte, with no further character-class restriction; a UUID is allowed but not required. It is never semantic content.

The initial implementation permits one active Turn per Conversation. A concurrent second send receives a safe conflict (`409 conversation_busy`). A transaction allocates a monotonic Conversation sequence and enforces unique `(conversation_id, sequence)` and `(conversation_id, client_message_id)`; ownership and AgentInstance predicates are checked in the same short transaction. Provider/network work never occurs inside that transaction.

### Run context snapshot

- **F1** creates Conversations/Turns/Messages but does not implement multi-turn ContextBuilder semantics; conversational Runs may still use the existing ordinary Run input snapshot.
- **F2 onward** requires that **every** conversational Run that uses ContextBuilder has **exactly one** durable `RunContextSnapshot`, written atomically with that Run/Job submission. Worker execution and recovery use that snapshot and never rebuild context from mutable live Conversation/Memory state. Non-conversational Stage-E and manual Runs do not require it.

The snapshot MUST contain the actual provider-neutral rendered text/context used for execution, not merely references; message IDs, compaction version, and memory versions are audit/provenance metadata attached alongside immutable content. NervOS never injects secret-store credentials into the snapshot merely as context, but the snapshot may contain sensitive user-provided content used by the Run, so historical-deletion caveats (ADR 0023) apply.

### Summary generation

Stage-F V1 Conversation compaction is deterministic and non-model; it MUST NOT call any model/provider and is derived, versioned, provenanced by source sequence range, bounded, and rebuildable. If it is unavailable or stale, normal bounded-history execution remains possible with no summary. LLM-generated semantic summaries require a reviewed change request and, if approved, must use the ordinary NervOS Run+Job path, never a direct provider call.

### Run integration

Conversational submission always goes through the existing ordinary `Run + Job` insertion seam (`AgentService.submit_run` and `insert_run_and_job_on_connection`). Conversation code does not create a worker, queue, retry engine, ToolLoop, or model abstraction. Scheduled, webhook, and event Runs remain valid with no Conversation link.

### Deletion and historical execution

Conversation archive and delete are application lifecycle operations. They do not casually cascade-delete Runs, Jobs, Attempts, or audit events. Deleting active conversation state prevents future context retrieval; immutable historical execution records and snapshots follow the retention policy in ADR 0023.

## Consequences

This prevents duplicate user messages while allowing multiple execution Runs for one conversational Turn, makes assistant finalization crash-safe without re-running the model, removes message-ordering races, and makes conversational execution recoverable from a durable snapshot regardless of later Conversation/Memory edits. It preserves Stage C retry/recovery, Stage D tools, and Stage E conversationless automation. Streaming, optimistic concurrent ordering, cross-agent sharing, and a separate durable AgentSession are outside F1 unless a reviewed change request is accepted.
