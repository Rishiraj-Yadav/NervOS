# ADR 0022 — Scoped Memory and Deterministic Context Assembly

Status: Accepted

## Context

NervOS has no delivered conversation or memory tables. Existing target intent requires private-by-default context, bounded history, scoped memory, and a provider-neutral path into the existing model/tool execution. Memory and retrieved text are untrusted data: they must not become system instructions or alter Stage-D permission decisions. Conversation execution must also remain possible and recoverable regardless of later Conversation or Memory edits, and independent of any summarizing capability.

## Decision

### ContextBuilder authority

Create one provider-neutral ContextBuilder application service. It selects and orders context before the ordinary Run is submitted; providers and the frontend do not independently retrieve history or memory. Its output is captured in a durable `RunContextSnapshot` (below) written atomically with the Run so conversational execution can execute, retry, or recover from the exact immutable context even after later edits.

Authority order is:

1. AgentDefinition and NervOS system/policy instructions;
2. explicitly approved USER profile context;
3. approved AGENT memory;
4. versioned deterministic Conversation compaction (optional accelerant);
5. deterministic recent USER/ASSISTANT messages;
6. current USER message and other retrieved data.

Conversation, memory, tool output, and external retrieval remain data at every level. None can grant or change a ToolGrant, bypass permission checks, disclose a secret, or override policy. Existing live tool permission evaluation remains authoritative.

### Mandatory Run context snapshot (F2 onward)

- **F1** creates Conversations/Turns/Messages but does not implement multi-turn ContextBuilder; conversational Runs may still use the existing ordinary Run input snapshot.
- **F2 onward** requires that **every** conversational Run that uses ContextBuilder has **exactly one** durable `RunContextSnapshot`, written atomically with that Run/Job submission. Worker execution and recovery use that snapshot and **never rebuild context from mutable live Conversation/Memory state**. Non-conversational Stage-E and manual Runs do not require this table.

The durable snapshot MUST contain the **actual provider-neutral rendered text/context used for execution**, not only mutable references; message IDs, compaction version, and memory versions are audit/provenance metadata attached alongside the immutable rendered content. On credentials: NervOS never injects secret-store credentials into the snapshot merely as context, but the snapshot **may contain sensitive user-provided content used by the Run**, so the historical-deletion caveats in ADR 0023 apply to it.

### Frozen V1 context bounds

Concrete initial bounds, aligned to the inspected runtime limits and changeable only by a reviewed change request:

| Bound | Value | Source |
|---|---|---|
| Conversation title | ≤ 100 code points | existing `display_name` max 100 |
| Current USER message | ≤ 8000 bytes / 4000 code points | `RunLimits.input_*` |
| Recent-message window | ≤ 20 messages | default page size 20 |
| Stored compaction | ≤ 32000 bytes, one current version | `output_max_bytes=32000` |
| Memory item | ≤ 32000 bytes | `output_max_bytes=32000` |
| Memory items per scope | ≤ 1000 | hard active count |
| Retrieval candidates | ≤ 50 | `PageLimit` le=50 |
| Injected memory bytes | ≤ 4000 | bounded subset of total |
| Total assembled context | ≤ 8000 bytes / 4000 code points | ordinary Run `input_*` bound |
| Page/search limit | ≤ 50, default 20; keyset `before_id` | existing route convention |

Exact provider tokenizers are deferred; the byte/code-point bounds are the frozen authority. Oversized current input is rejected; older context is trimmed by explicit priority and stable sequence/ID tie-breakers; assembly always terminates within the total bound.

### V1 summary — deterministic, non-model compaction (frozen)

Stage-F V1 summary/compaction is **deterministic and non-model only**. It may be generated from older committed USER/ASSISTANT messages, and it **MUST NOT call any model/provider**. It is derived data, versioned, provenanced by source sequence range, bounded, and rebuildable. If it is unavailable, stale, or failing, the ContextBuilder uses bounded recent history with no summary and the Run executes normally. LLM-generated semantic summaries are **not** part of Stage-F V1; a future semantic-summary feature requires a `STAGE F ARCHITECTURE CHANGE REQUEST` and, if approved, must use the ordinary NervOS Run+Job execution path, never a direct provider call.

### Scopes and write authority

Durable `MemoryItem` scopes in V1 are `AGENT` (one owner and AgentInstance) and `USER` (owner-wide approved profile facts). There is **no separate Conversation `MemoryItem` in V1**: Conversation context is Messages + deterministic compaction. Any distinct Conversation-memory semantic requires external approval.

Arbitrary model, tool, external, or summary text is never automatically promoted to AGENT or USER memory. **Permanent AGENT and USER memory writes require explicit user action or an explicitly user-approved promotion path; there is no automatic permanent-memory extraction in Stage-F V1.** AGENT memory writes use NervOS application services. No memory scope authorizes tools.

### Retrieval technology

V1 uses SQLite indexes and deterministic bounded retrieval. FTS5 is conditional on supported-runtime portability validation and is a rebuildable projection, never authority. A provider-neutral `MemoryRetrievalPort` is the extension seam for later hybrid/vector retrieval. Stage F adds no vector database, embedding service, or external retrieval dependency.

### Dependency boundary

Agents/packages receive provider-neutral application protocols, not SQLAlchemy Sessions, ORM records, or raw database handles. Context and memory data remain in the NervOS runtime database and are not stored inside an Agent package.

## Consequences

The same ContextBuilder contract works for tool-enabled and tool-free Runs, preserves model-provider portability, makes retrieval auditable and bounded, and lets any conversational Run execute or recover from its own durable snapshot. It adds snapshot/query/index work and requires deterministic invalidation of compaction and search projections. The initial design intentionally excludes a Conversation memory scope, automatic permanent-memory extraction, LLM semantic summaries, workspace collaboration, vector infrastructure, and streaming.
