# NervOS Memory Architecture

## Status

Target architecture. Agent memory is not implemented in Stage A.

## Principle

Memory is private by default and shared intentionally. NervOS must not automatically merge all memories from every agent owned by a user.

## Memory scopes

### Session memory

Short-term conversational context for one session: recent messages, current task details, temporary working context.

### Agent-private memory

Persistent information available only to one user's AgentInstance, such as Research Agent report preferences or Invoice Agent processing state.

### User profile memory

Explicit user-wide information that approved agents may use, such as timezone, language, and preferred name. This must not become a dump of every fact learned by every agent.

### Shared workspace memory

Explicit collaboration scope such as `Project Alpha`. Only agents granted access may read/write it.

### Run trace

Tool calls, model calls, errors, and run events are audit/runtime history, not long-term semantic user memory.

## Why not combine everything

Automatic cross-agent memory creates privacy leaks, irrelevant context, higher token cost, behavior coupling, and difficult deletion/ownership semantics.

Example: confidential information learned by an Email Agent should not automatically become visible to a Social Media Agent.

## Context construction

Future model-driven context should contain only what is needed:

```text
agent/system instructions
+ configuration
+ approved user profile
+ relevant agent-private memories
+ authorized shared-workspace memories
+ session summary
+ recent messages
+ current request
```

Never send the full historical memory/message database blindly.

## Memory writing policy

Not every message becomes permanent memory. A future memory writer should consider usefulness, scope, sensitivity, duplication, confidence/source, expiration, and user control.

Potential memory types: preference, fact, decision, task, summary, artifact reference.

## User controls

Users should eventually be able to inspect, edit, delete, clear, export, and disable automatic memory writing, and manage which agents can access shared memory.

## Storage progression

Start with SQLite tables, summaries, indexes, and optional FTS. Add embeddings/vector retrieval only when it materially improves retrieval; a vector database is not a first-version requirement.
