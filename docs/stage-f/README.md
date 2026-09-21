# Stage F — Conversations, Context, and Memory

**STATUS: ACCEPTED — F0 ARCHITECTURE FROZEN**

**BASELINE: Stage-E merge `ec80c2319b4cd15b9a1e8337923841c58ce57642`**

This is the canonical Stage-F master plan and the frozen Stage-F architecture authority. Every Stage-F implementation milestone MUST read this document and all accepted Stage-F ADRs before changing code. This revision incorporates the authorized F0 external-review correction pass, the F0 final architecture cleanup pass, and external F0 acceptance. F0 is architecture and planning only; it does not implement conversations, memory, migrations, APIs, UI, or dependencies. No migration is created in F0; the real migration head remains `0008`.

## Verification protocol

At the start of every F1+ turn:

```text
git hash-object docs/stage-f/README.md
```

The report MUST include `Stage-F plan blob: <hash>`, the accepted ADRs read, the current milestone, and predecessor status. A conflict with this plan or an accepted ADR is a stop condition:

```text
STAGE F ARCHITECTURE CHANGE REQUEST — <issue>
```

Do not silently edit the plan while implementing. External review must accept F0 before F1 begins. The concrete context bounds and the rules below are frozen; changing any requires a reviewed change request.

## Current implementation truth and boundaries

Stage E is complete. The current runtime has owner-scoped `AgentInstance` records identified by exact `agent_key` and `agent_definition_version`; immutable `Run` snapshots with one `input_text` and one final `output_text`; one `Job` per Run; one-or-more leased `Attempt`s; and the existing Worker, `RunExecutor`, `ModelRequest`, and Stage-D `ToolLoop`. The canonical ordinary execution seam is `AgentService.submit_run` → `insert_run_and_job_on_connection`, which atomically creates the Run and Job. Stage E schedule, webhook, and event executions remain ordinary Runs and do not require a conversation.

Authentication sessions (`auth_session`) identify who is logged into NervOS. They are not conversations. The existing server-side opaque cookie/session and Origin boundary remain unchanged. Future conversation APIs use `CurrentUserDependency`, `OriginDependency`, owner-first services, IDOR-collapsed not-found responses, keyset pagination, and the existing `/api/v1` and TanStack Query conventions.

No conversation, turn, message, summary, or memory runtime tables exist today. `docs/sessions.md`, `docs/memory.md`, and ADR 0005 are target intent, not delivered behavior. Stage F does not implement Stage G package installation, Stage H secret/sandbox isolation, or Stage J collaboration.

## Core mental model

```text
NervOS runtime
├── AgentInstance A (owner-scoped)
│   ├── Conversations
│   └── private AGENT memory
├── AgentInstance B (owner-scoped)
│   ├── Conversations
│   └── private AGENT memory
└── AgentInstance C (owner-scoped)
    ├── Conversations
    └── private AGENT memory

Shared infrastructure: database, Runs, Jobs, Workers, models, tools, scheduler.
Not automatically shared: conversation history, AGENT memory, or USER memory.
```

A Conversation belongs to exactly `(owner_user_id, agent_instance_id)`. One AgentInstance may have many Conversations. Another owner or another AgentInstance, even under the same owner, cannot read it unless a future explicit sharing mechanism grants access. Automated schedule/webhook/event Runs may have `conversation_id = NULL`.

## Frozen F0 conversation protocol (proposed)

ADR 0021 is the authority for these draft decisions.

### Turn, message, and Run linkage

```text
Conversation
  └── ordered ConversationTurn
        ├── exactly one durable USER message
        ├── one or more Run links (initial or retry)
        └── at most one authoritative ASSISTANT message

Turn → ordinary Run → ordinary Job → existing Worker/Attempt → Model/Tools
```

A user message is persisted once before submission. Automatic Stage-C retry and manual retry never duplicate it; a manual retry creates a new ordinary Run linked to the same turn. Durable roles are `USER` and `ASSISTANT`; tool calls/results remain Stage-D `ToolInvocation` audit data.

### History ordering (frozen)

Message order is derived, not independently allocated:

```text
ORDER BY turn.sequence, role_order
  where role_order: USER = 0, ASSISTANT = 1
```

There is **no** independently allocated conversation-wide message sequence. Because a Turn has exactly one USER (role 0) and at most one ASSISTANT (role 1), a recovered assistant message for Turn N always appears after that Turn's USER message and before Turn N+1. Retained invariants: one USER per Turn, at most one ASSISTANT per Turn, one authoritative successful Run per Turn.

### Run status and Turn finalization are distinct steps

Run execution status and Conversation Turn finalization are separate. A Run may reach `SUCCEEDED` while the Conversation projection is not yet finalized. For a conversational Turn:

```text
Run reaches SUCCEEDED
  ↓  ONE short DB transaction (idempotent finalizer)
     CAS/select authoritative_run_id
   + insert exactly one ASSISTANT message
   + transition Turn to SUCCEEDED
  ↓
Commit
```

Until that finalizer transaction succeeds, the Turn remains **active/non-finalized for Conversation sequencing**, so another Turn cannot be accepted yet. If the process crashes after Run success but before finalization, recovery re-runs **only the idempotent finalizer** and never re-runs the model. The first Run to transition the Turn to `SUCCEEDED` under this guarded (CAS) transition is the single authoritative Run; a later successful Run cannot become a second authoritative projection.

### Turn state machine and manual retry

A Turn has `PENDING`/`RUNNING` and terminal `SUCCEEDED`, `FAILED`, `CANCELLED`, `AMBIGUOUS`. Retry rules are frozen:

- The **latest** `FAILED` or `CANCELLED` Turn may be manually retried (new Run link, same USER message).
- A `SUCCEEDED` Turn cannot be manually retried.
- An `AMBIGUOUS` Turn gets no ordinary one-click same-turn retry; it is surfaced as ambiguous and the user sends a new message to continue.
- A non-latest, older Turn cannot be manually retried once a later Turn exists.

Stage-C retry/lease mechanics remain the existing authority; Stage F does not add a retry or lease engine.

### Submission identity and concurrency

`client_message_id` is the durable domain submission identity: an owner+Conversation-scoped value stored on the Turn with a canonical content digest. Same ID plus same content replays the existing Turn/result; same ID plus different content is a conflict; a different ID creates a new Turn.

**`client_message_id` validation contract (frozen, aligned to existing repository convention).** It is an opaque client-generated identifier, not semantic content, validated like the existing webhook idempotency key (`packages/nervos-core/src/nervos_core/domain/triggers.py`):

| Property | Value |
|---|---|
| Type | non-empty string (UTF-8), opaque client-generated |
| Minimum length | 1 |
| Maximum length | 128 characters (mirrors `MAX_IDEMPOTENCY_KEY_LENGTH = 128`) |
| Allowed syntax | any non-empty text except a NUL (`\x00`) character; no further character-class restriction |
| UUID | allowed but **not required**; any opaque value within the length/NUL rule is accepted |

The digest is computed from canonical content, so the ID itself carries no semantics. This mirrors the existing `event_id`/idempotency-key shape rather than inventing a new one.

Initially one active Turn is allowed per Conversation. A concurrent second send receives a safe `409 conversation_busy`. A transaction allocates a monotonic Conversation sequence and enforces unique `(conversation_id, sequence)` and `(conversation_id, client_message_id)`; ownership and AgentInstance predicates are checked in the same short transaction. Provider/network work never occurs inside that transaction.

### Run context snapshot — mandatory from F2 onward

- **F1:** Conversations/Turns/Messages exist. Conversational Runs may still use the existing ordinary Run input snapshot. F1 does **not** implement multi-turn ContextBuilder semantics.
- **F2 onward:** **every** conversational Run that uses ContextBuilder MUST have exactly one durable `RunContextSnapshot`, written atomically with that Run/Job submission. Worker execution and recovery use that snapshot and **never rebuild context from mutable live Conversation/Memory state**.
- Non-conversational Stage-E and manual Runs do not require this table.

The durable snapshot MUST contain the **actual provider-neutral text/context used for execution**, not only mutable references. Message IDs, summary version, and memory versions are audit/provenance metadata attached alongside the immutable rendered content, never a substitute for it. Credentials are excluded as follows: NervOS never injects secret-store credentials into the snapshot merely as context; the snapshot **may contain sensitive user-provided content used by the Run**, so historical-deletion caveats (ADR 0023) apply to it.

### Run integration

Conversational submission always goes through the existing ordinary `Run + Job` insertion seam (`AgentService.submit_run` and `insert_run_and_job_on_connection`). Conversation code does not create a worker, queue, retry engine, ToolLoop, or model abstraction. Scheduled, webhook, and event Runs remain valid with no Conversation link.

### Deletion and historical execution

Conversation archive and delete are application lifecycle operations. They do not casually cascade-delete Runs, Jobs, Attempts, or audit events. Deleting active conversation state prevents future context retrieval; immutable historical execution records and snapshots follow the retention policy in ADR 0023.

## ContextBuilder

One provider-neutral application service owns context selection. Providers do not independently load history or memory, and the frontend never builds model context.

```text
AgentDefinition/system policy
 + approved USER profile context
 + approved AGENT memory
 + versioned Conversation compaction (deterministic, below)
 + deterministic recent USER/ASSISTANT messages
 + current USER message
        ↓ bounded ContextBuilder snapshot (RunContextSnapshot, F2+)
        ↓ existing ModelRequest (system_instruction, user_text, turns, tools)
        ↓ existing RunExecutor / ToolLoop
```

Authority classes are explicit: AgentDefinition and NervOS policy are highest; all conversation, memory, tool output, and retrieved external content are data. Stored text cannot grant a tool, change a ToolGrant, bypass permission checks, disclose a secret, or override policy. The Stage-D live permission evaluator and Run grant cutoff remain authoritative.

### Concrete frozen context bounds (F0)

Frozen initial constants for Stage F V1, chosen to align with the existing inspected bounds; changing any requires a reviewed `STAGE F ARCHITECTURE CHANGE REQUEST`.

| Bound | Frozen value | Source / rationale |
|---|---|---|
| Conversation title | ≤ 100 code points | Aligns with existing `display_name` max 100 (`apps/api/src/nervos_api/api/schemas.py`, MCP connections) |
| Current USER message | must fit the Run input bound: ≤ `8000` bytes / `4000` code points | Existing `RunLimits.input_max_bytes=8000` / `input_max_code_points=4000` (`packages/nervos-core/src/nervos_core/domain/runs.py:81-82`); oversize is rejected |
| Recent-message history window | ≤ 20 messages | Bounded deterministic history; aligns with default page size 20 |
| Stored compaction | ≤ `32000` bytes, one current per Conversation, version-gated | Aligns with `output_max_bytes=32000`; stored form may exceed assembled context and is trimmed at assembly |
| Memory item size | ≤ `32000` bytes | Aligns with `output_max_bytes=32000` |
| Memory items per scope | ≤ `1000` | Hard count cap; active only |
| Retrieval candidates | ≤ `50` candidates | Aligns with `PageLimit` `le=50`; then item/byte caps |
| Injected memory bytes (into context) | ≤ `4000` bytes across selected items | Bounded subset of the total context bound; trimmed by priority |
| Total assembled context bytes | ≤ `8000` bytes / `4000` code points | Must fit the ordinary Run `input_*` bound; the assembled ContextBuilder output becomes the Run `input_text` |
| `client_message_id` | non-empty, ≤ 128 chars, no NUL; opaque, UUID allowed not required | Mirrors existing `MAX_IDEMPOTENCY_KEY_LENGTH`/`event_id` shape |
| Pagination / search page limit | ≤ `50`, default `20`; `before_id > 0` keyset | Existing `PageLimit`/`BeforeId` convention in routes |

Exact provider tokenizers are deferred; the byte/code-point bounds above are the frozen authority. Older context is deterministically trimmed by explicit priority then stable sequence/ID tie-breakers; assembly always terminates within the total bound. Oversized current input is rejected rather than trimmed.

### V1 summary — deterministic, non-model compaction (frozen)

Stage-F V1 summary/compaction is **deterministic and non-model only**. F2 may generate bounded deterministic Conversation compaction from older committed USER/ASSISTANT messages. It **MUST NOT call any model/provider**. It is derived data, versioned, provenanced by source sequence range, bounded, and rebuildable. If it is unavailable, stale, or failing, the ContextBuilder uses bounded recent history with no summary and the Run executes normally.

**LLM-generated semantic summaries are NOT part of Stage-F V1.** A future semantic-summary feature requires a `STAGE F ARCHITECTURE CHANGE REQUEST`; if approved it must use the ordinary NervOS Run+Job execution path and never a direct provider call.

## Memory policy and lifecycle

ADR 0022 covers assembly and retrieval; ADR 0023 covers lifecycle.

**V1 scopes.** Durable `MemoryItem` scopes are `AGENT` = `(owner, agent_instance)` and `USER` = owner-wide approved profile. There is **no separate Conversation `MemoryItem` in V1**: Conversation context in V1 is **Messages + compaction**, and a distinct Conversation-memory semantic may be introduced only if externally approved later. No global memory is visible to all owners or agents. A future `WORKSPACE` scope requires explicit membership and Agent authorization and belongs to Stage J.

**Write authority.** Permanent `AGENT` and `USER` memory writes require explicit user action or an explicitly user-approved promotion path. There is **no automatic permanent-memory extraction in Stage-F V1**: arbitrary model, tool, external, or summary text is never silently promoted to AGENT or USER memory. AGENT memory writes go through NervOS memory application services; conversation history is durable but is not automatically permanent memory. Every item records owner, scope, source type, source message/Run where applicable, created/updated version, inferred/user-authored marker, supersession, and deletion state. Memory is data, never authority.

## Proposed data model (no migration in F0)

All tables are in the NervOS runtime database, not an Agent package. Milestone ownership below is frozen; exact column spelling is an implementation target for the owning milestone and requires ADR review before migration.

| Table | Milestone | Purpose and key fields | Constraints, indexes, deletion |
|---|---|---|---|
| `conversations` | F1 / 0009 | `id`, `owner_user_id`, `agent_instance_id`, title/status, created/updated/last_active/archived/deleted timestamps | FK owner/Agent; owner+id and owner+Agent indexes; active list excludes deleted |
| `conversation_turns` | F1 / 0009 | `id`, `conversation_id`, monotonic `sequence`, state machine state, `client_message_id`, canonical content digest, created/finished timestamps | unique `(conversation_id, sequence)` and `(conversation_id, client_message_id)`; one active Turn per Conversation; keyset sequence index |
| `conversation_messages` | F1 / 0009 | `id`, `conversation_id`, `turn_id`, role USER/ASSISTANT, content, created time, source Run | one USER (role 0) and at most one authoritative ASSISTANT (role 1) per Turn; **ordering by `(turn.sequence, role_order)`**, no independent conversation-wide sequence; deletion policy explicit |
| `conversation_run_links` | F1 / 0009 | turn, ordinary `run_id`, ordinal, role (initial/retry), authoritative flag | unique `(turn_id, run_id)`; at most one authoritative successful Run per Turn; Run remains canonical execution record; no second executor |
| `run_context_snapshots` | F2 / 0010 | 1:1 `run_id`, **actual provider-neutral rendered context text used for execution**, plus audit metadata (message IDs, summary version, memory IDs/versions, AgentDefinition identity/version), bounds and digest, created time | unique `(run_id)`; mandatory for every ContextBuilder conversational Run; immutable; written atomically with submission; non-conversational Runs do not require it |
| `conversation_compactions` | F2 / 0010 | conversation, version, source start/end sequence, deterministic content, digest, created time, superseded/deleted state | unique `(conversation_id, version)`; one current-compaction index; stale detection by source end; deterministic non-model only |
| `memory_items` | F3 / 0011 | owner, scope AGENT/USER, Agent nullable scope ID, type, current version, source/provenance, status, timestamps | CHECK scope shape; owner-scoped indexes; no cross-Agent default path; no Conversation scope in V1; active/current index |
| `memory_versions` | F3 / 0011 | item/version, value, authored/inferred marker, source message/Run, digest, superseded/deleted timestamps | unique `(item_id, version)`; current-only retrieval; historical provenance retained per policy |
| `memory_fts` (conditional) | F3 / 0011 | searchable projection of active current versions | only if FTS portability validated; synchronized transactionally or rebuildable; never authoritative |

Every foreign-key query includes the owner and required Agent/Conversation scope. No Agent/package receives SQLAlchemy sessions, ORM rows, or raw database handles; application protocols mediate persistence and retrieval.

## Query and migration plan

Expected indexed paths: owner Conversation list `(owner_user_id, id)`, Conversation detail `(owner_user_id, id)`, Turn keyset `(conversation_id, sequence)`, Turn idempotency `(conversation_id, client_message_id)`, active Turn `(conversation_id, state)`, message read by `(turn_id, role_order)` with `turn.sequence` order, turn→Run links `(turn_id, ordinal)`, Run→context snapshot 1:1 `(run_id)`, Run→Turn provenance `(run_id, turn_id)`, Agent memory `(owner_user_id, agent_instance_id, current/status)`, USER memory, compaction current/version, and active FTS projection. Growing collections use keyset pagination; bounded retrieval uses a capped candidate query, never unbounded offset scans.

**Frozen milestone/migration ownership:**

- **F1 — migration `0009`** contains only durable Conversation execution-protocol structures: `conversations`, `conversation_turns`, `conversation_messages`, `conversation_run_links`. F1 implements owner/Agent isolation, the Turn state machine, `client_message_id`, the one-active-Turn rule, ordinary Run submission, manual retry, authoritative Run selection, assistant finalization/recovery, and the Conversation API/minimal UI.
- **F2 — next migration, expected `0010`** contains `run_context_snapshots` and Conversation compaction/summary structures if persistence is required. F2 implements the ContextBuilder, the immutable structured context snapshot, bounded history, deterministic non-model compaction, and context ordering/budget.
- **F3 — later migration only if required, expected `0011`** contains `memory_items`, `memory_versions`, and `memory_fts` only if FTS portability is validated. F3 owns AGENT memory, USER memory, provenance, retrieval, deterministic ranking, and memory security. "Conversational retries/leases" is removed from F3 scope except for regression/inheritance testing; Stage-C retry/lease mechanics remain existing authority.
- **F4** — lifecycle/delete/UI. **F5** — integrated acceptance only.

Exact boundaries must follow the existing Alembic convention and preserve `0001`–`0008`. F0 creates none; the real head remains `0008`, and no migration number is claimed as accepted until its milestone lands. Downgrades follow repository policy and must not weaken historical migration protection.

## Proposed API and frontend boundary

Candidate authenticated routes are `/api/v1/conversations`, `/api/v1/conversations/{id}`, `/api/v1/conversations/{id}/messages`, and `/api/v1/memories`, subject to source review in F1. Writes require the configured Origin and derive owner from the session; requests never accept `owner_user_id`. Conversation sends create/use a Turn with `client_message_id`, then submit an ordinary Run; routes never call providers. Responses expose safe IDs, status, sequence, timestamps, bounded content, provenance labels, and keyset cursors (`items`, `next_before_id`); errors include not-found collapse, busy (`409 conversation_busy`), idempotency conflict, stale-version conflict, and safe validation/context-too-large codes without SQL/provider text/tracebacks/secrets.

The UI reuses `SessionGate`, the existing fetch wrapper and `/api/v1` proxy, TanStack Query owner-safe keys, keyset pagination, cache invalidation, and existing Run status/timeline components. F1 provides Conversation list/detail, send, and visible failed/cancelled/ambiguous turn state. F4 adds bounded memory inspection/edit/delete and source labels. Logout/account change clears private query caches; private memory is not stored in localStorage. Streaming is out of scope unless separately accepted; ephemeral tokens must never become the durable assistant message. No workflow builder, multi-agent chat, package UI, or collaboration UI is included.

No durable artifact subsystem is invented. If a future artifact domain exists, F4 specifies only authorized Conversation/Run relationships and lifecycle; clients never receive arbitrary filesystem paths.

## Milestones

### F0 — architecture and safety freeze (this document)
External review of this README and draft ADRs 0021–0023. No runtime/schema change, no migration. Exit: decisions, matrices, plan hash, and boundaries accepted.

### F1 — Conversations, messages, turns, and linkage (migration `0009`)
Create the Conversation execution-protocol schema and ports; implement owner/Agent isolation, one active Turn, sequence allocation, `client_message_id` validation/idempotency, the Turn state machine and manual retry rules, assistant finalization as an idempotent single-transaction projection with crash-after-Run-success recovery, one CAS authoritative successful Run per Turn, and the derived `(turn.sequence, role_order)` history ordering. Implement Conversation management/history API and minimal UI; link Turns to ordinary Run submission without changing Worker/Attempt/ToolLoop. F1 does not implement multi-turn ContextBuilder; conversational Runs may still use the existing ordinary Run input snapshot. Prove duplicate HTTP submission, concurrent send race, restart durability, manual retry without duplicate USER message, failed/cancelled/ambiguous Turn states, no re-run on crash finalization, ordering correctness, query plans, migration integrity, and conversationless Stage E Runs.

### F2 — Context snapshots and deterministic compaction (expected migration `0010`)
Implement the provider-neutral ContextBuilder under the frozen bounds, authority classes, bounded recent history, deterministic non-model compaction generated only from committed messages (never a provider call) with bounded-history fallback, and the mandatory immutable `RunContextSnapshot` (actual rendered context text plus audit metadata) written atomically with submission and used for execution/recovery. Prove ordering, bounds, compaction fallback/staleness, Run immutability, conversational execute/recover after Conversation/Memory change, tool-enabled conversational execution, and no model/provider-specific history loading.

### F3 — Scoped memory and deterministic retrieval (later migration only if required, expected `0011`)
Implement AGENT and explicit USER memory services, provenance/versioning, no Conversation scope, bounded SQLite/indexed retrieval, and a validated FTS abstraction (only if FTS portability is validated). Prove owner/Agent isolation, deterministic ranking, caps, write authority (explicit action only), no automatic extraction, no memory-authorized tools, and Stage-C retry/lease regression inheritance only.

### F4 — Lifecycle controls
Implement inspect/edit/version/delete, index and compaction invalidation, archive/delete distinction, retention controls and minimal memory UI. Prove deleted data leaves active reads, FTS, compaction and context; stale edit conflicts; historical Run/snapshot/backup caveats; and safe Conversation deletion without casual Run/Job cascade.

### F5 — integrated acceptance and closeout
No new feature. Prove multi-turn follow-up, idempotency, restart, retries, failure semantics, snapshot recovery, isolation, budget, compaction and fallback, deletion invalidation, tool loop, query plans, migration integrity, frontend journey, full local regression, hosted CI, and documentation truth. Close Stage F only after all accepted criteria pass.

## Security matrix

| Threat | Required boundary/behavior | Proof milestone |
|---|---|---|
| Cross-owner or cross-Agent Conversation/memory leak | owner + Conversation + Agent predicates; 404 collapse | F1/F3 |
| Unauthorized AGENT/USER memory | explicit user action/promotion only; no auto-extraction | F3 |
| Prompt injection persisted as memory | data-only rendering; never system authority; explicit promotion only | F2/F3 |
| Memory/tool escalation | ToolGrant evaluator remains sole authority | F2/F3 |
| Stale deleted FTS/compaction | transactional invalidation/current-version filter | F4 |
| Duplicate send/retry/order race | `client_message_id` idempotency + sequence/CAS | F1 |
| Run context drift | mandatory immutable `RunContextSnapshot` (F2+) | F1/F2 |
| Snapshot sensitive-content exposure | no injected credentials; user content caveat documented; retention policy | F2/F4 |
| Direct-model summary bypass | V1 compaction is deterministic/non-model only | F2 |
| Context/memory DoS | frozen byte/count/candidate limits | F2/F3 |
| Secret persistence/log exposure | no raw content by default; safe IDs/counts/sizes | F3/F5 |
| IDOR/deletion leak/package update confusion | owner services, safe errors, AgentInstance ownership, package/runtime separation | F1/F4/F5 |

## Recovery, concurrency, and lifecycle matrix

| Scenario | Durable expectation | Proof |
|---|---|---|
| API restarts after USER message commit | `client_message_id` replay finds one Turn; no duplicate message | F1/F5 |
| Crash after Run `SUCCEEDED`, before finalizer | re-run only the idempotent finalizer; never re-run the model | F1/F5 |
| Two Runs reach SUCCEEDED on one Turn | CAS picks one authoritative; one assistant; no second projection | F1 |
| Finalizer not yet committed | Turn stays active/non-finalized; another Turn not accepted | F1 |
| Run queued or worker crashes pre-start | existing reclaim/retry semantics; Turn held at retry boundary | F1/F3/F5 |
| Latest FAILED/CANCELLED Turn | manual retry allowed (new Run, same USER message); older Turn blocked after later Turn | F1 |
| SUCCEEDED Turn | cannot be manually retried | F1 |
| Ambiguous post-start loss | no one-click same-turn retry; surfaced as ambiguous; new message to continue | F1/F5 |
| Conversation/Memory edited while Run queued | queued Run executes/recovers from its `RunContextSnapshot` (F2+) | F2 |
| Compaction refresh race or failure | version/source-range check; bounded-history fallback; one current winner | F2 |
| Conversation delete during active Run | no casual execution-row cascade; active context unavailable after policy boundary | F4/F5 |

Lifecycle authority: Conversations are created by the authenticated owner and archived/deleted through services; messages are created by Turn submission and assistant finalization; Turns link Runs but do not replace Run audit; compaction is derived/versioned with bounded-history fallback; memory is versioned active data written only via explicit paths; FTS is a rebuildable projection; historical Runs/snapshots/audit follow existing retention. All reads are owner/Agent scoped and bounded.

## Future-proofing and explicit non-goals

| Future feature | F prepares | F does not implement |
|---|---|---|
| LLM semantic summaries | deterministic compaction structure + change-request seam | any V1 model-generated summary or direct provider call |
| Vector/hybrid memory | `MemoryRetrievalPort`, stable item/provenance model | vector DB, embeddings, external service |
| Shared workspace / handoff | private scopes and explicit future WORKSPACE seam | Stage-J membership/collaboration |
| Package upgrades | Conversation attaches to AgentInstance; Run snapshots identity | Stage-G installer/update/uninstall |
| Remote/Postgres | persistence/application ports | Postgres deployment |
| Streaming/mobile | durable post-success assistant semantics and API boundary | streaming transport/mobile UI |
| Distinct Conversation-memory | V1 is Messages+compaction only; seam for later approved semantic | automatic Conversation MemoryItem |
| Encrypted memory/backup | deletion/retention caveats | secret manager, backup system |

NervOS runtime data remains at `~/.nervos/nervos.db`; Agent packages are software/definitions. Stage G owns `.nervos` install/update/rollback and must not bundle or delete personal Conversation/memory data.

## Milestone-start checklist and change control

Before F1–F5: read this README and accepted Stage-F ADRs; run `git hash-object docs/stage-f/README.md`; report the hash; verify predecessor, migration head, clean tree; state hard in/out scope and expected production/schema changes; do not implement later milestones.

After F0 acceptance, any deficiency, including changes to the frozen context bounds, snapshot rule, finalizer transaction, history ordering, or deterministic-summary decision, requires `STAGE F ARCHITECTURE CHANGE REQUEST — <issue>` with existing decision, failure, proposed change, migration/API/security impact, and affected milestones. No silent plan edits. ADRs 0021–0023 are accepted and frozen; the Git blob of this README at the accepted commit is the F1 authority. F0 is an accepted architecture milestone only and is not a Stage-F runtime implementation.
