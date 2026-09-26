# NervOS Memory Architecture

## Status

**Stages A through F are complete.** Stage F implements scoped USER/AGENT memory. This document describes target architecture; `docs/implementation-status.md` is the authoritative delivered-state reference.

**What is implemented (Stage F):** scoped USER memory (owner-wide, user-approved profile), scoped AGENT memory (owner + specific AgentInstance), explicit direct creation and user-approved promotion from conversations, versioned immutable memory items with provenance tracking, deterministic bounded retrieval (≤50 candidates with keyset refill), active item limits (≤1000 per scope, ≤32000 bytes per item), memory injection sub-budget (≤4000 bytes), ContextBuilder v2 integration, immutable memory-bearing snapshots with v1 backward compatibility, single-attempt retry snapshot reuse, memory/tool authority isolation, zero automatic memory writes, memory inspection/editing/deletion API, conversation archive/unarchive/soft-delete, full minimal `/memories` UI, and private query cache hygiene on logout.

**What remains future (not implemented):** workspace/shared memory (deferred to Stage J), FTS/vector/embedding search, automatic memory extraction/writers, Conversation memory scope (V1 only USER/AGENT).

## Principle

Memory is private by default and shared intentionally. NervOS must not automatically merge all memories from every agent owned by a user.

## Memory scopes

### Session memory

Short-term conversational context for one session: recent messages, current task details, temporary working context.

### Agent-private memory (IMPLEMENTED in Stage F)

Persistent information available only to one user's AgentInstance, such as Research Agent report preferences or Invoice Agent processing state. Implemented as AGENT-scoped memory items with explicit user creation and deterministic retrieval.

### User profile memory (IMPLEMENTED in Stage F)

Explicit user-wide information that approved agents may use, such as timezone, language, and preferred name. This must not become a dump of every fact learned by every agent. Implemented as USER-scoped memory items with user creation and approval gating.

### Shared workspace memory

Explicit collaboration scope such as `Project Alpha`. Only agents granted access may read/write it. **NOT implemented** (deferred to Stage J).

### Run trace

Tool calls, model calls, errors, and run events are audit/runtime history, not long-term semantic user memory.

## Why not combine everything

Automatic cross-agent memory creates privacy leaks, irrelevant context, higher token cost, behavior coupling, and difficult deletion/ownership semantics.

Example: confidential information learned by an Email Agent should not automatically become visible to a Social Media Agent.

## Context construction

Stage F uses deterministic context assembly without model summarization:

```text
agent/system instructions
+ configuration
+ approved user profile (USER memory)
+ relevant agent-private memories (AGENT memory)
+ authorized shared-workspace memories (NOT implemented)
+ session summary (Conversation compaction)
+ recent messages (Conversation turns)
+ current request
```

Never send the full historical memory/message database blindly.

## Memory writing policy

Stage F implements **zero automatic memory writes**. Memory creation and promotion require explicit user action.

Potential memory types: preference, fact, decision, task, summary, artifact reference.

## User controls

Users can inspect, edit, delete, and manage memory through the `/api/v1/memories` API and the minimal `/memories` UI (Stage F4). Automatic memory writing is disabled; there is no such writer to disable.

## Storage progression

Stage F uses SQLite tables (`memory_items`, `memory_versions`) with indexed retrieval. FTS/vector/embedding search is NOT implemented.
