# Priority 1 — Package tools, context, memory, and runtime UI

Status: Approved by the user on 2026-10-03 and implemented as ADR 0031. Local verification is recorded in the completion section below; external acceptance remains separate.

## Goal

Make existing NervOS integration paths usable through the dashboard: package agents can use explicitly bound MCP tools, receive correctly structured context and selected memory, and propose useful facts for future runs. Add owner-controlled private automatic memory saving as a reviewed extension. Keep NervOS a general-purpose runtime; email is one possible workload, alongside research and other agents.

## Inspected authority and baseline

- `docs/implementation-status.md` remains authoritative. Stages C–G are delivered; Stage-I MVP is implemented with formal external/production acceptance pending; Stage H is now implemented and complete.
- Read the Stage-F master plan and accepted ADRs 0021–0023. F0–F5 are complete. This work is an extension, not an unfinished F milestone.
- Stage-F plan blob: `62cfc0a8039e233c82e2a30ff3fd59495e99395d`.
- Read the Stage-G package boundary, SDK, memory, and manifest tool requirements; ADR 0014 governs public execution observability.
- Current package execution resolves tool requests against built-in descriptors. It does not bind package requests to selected MCP connections.
- Package initialization passes rendered context, but does not populate SDK `AgentContext.memory`. SDK message roles are flattened into text by the model bridge.
- Conversational `Run.input_text` already contains assembled context. Separating current input must use the snapshot's `current_user_text`; passing that assembled input as a fresh message would duplicate context.
- Existing memory management supports explicit creation/promotion, versioned edits, soft deletion, bounded selection, and immutable conversational snapshots. Automatic permanent writes are explicitly forbidden by current F/G contracts.
- Worker liveness persistence exists, but the dashboard has no connection, grant, tool-audit, or worker-health management surface.
- Preserve the existing uncommitted package-model fallback fix and all unrelated user changes. Reconcile stale roadmap language with delivered status without upgrading pending acceptance to accepted.

## Governance gates before implementation

### STAGE F ARCHITECTURE CHANGE REQUEST — Opt-in private memory writes and context for independent runs

Current ADRs 0022–0023 require explicit per-item user action and exempt independent runs from mandatory context snapshots. Proposed changes:

1. Add an explicit per-instance policy permitting bounded agent proposals and optional automatic saving into that same instance's private AGENT scope.
2. Keep USER memory writes user-reviewed; installation, manifests, model output, and connection setup cannot enable a writing policy.
3. Capture selected memory and current input for independent manual/scheduled runs through an immutable execution-context contract. Do not fabricate conversation turns for independent runs or replay their earlier run history.
4. If generic model-assisted fact extraction is enabled, execute it as an ordinary bounded Run/Job, with explicit cost disclosure. It does not replace deterministic conversation compaction.

Impact: policy/proposal/provenance persistence, snapshot schema compatibility, API/UI controls, finalization and recovery behavior. Existing retrieval, scope, ownership and context bounds remain unchanged. Frozen master plans and accepted ADRs must not be silently rewritten; record reviewed successor decisions before changing these invariants.

### Stage-G architecture change request — Portable MCP declarations and versioned host/context delivery

V1 machine-enforced manifest requirements name built-in tools only. Define a versioned extension for package-local tool aliases, upstream MCP tool expectations, and canonical schema fingerprints. Node-local connection/definition IDs belong exclusively to owner-selected instance bindings, never to portable manifests. Pin binding evidence for each accepted run and enforce live grants separately. Preserve V1 archive/manifest interpretation and verified historical artifacts.

SDK/host negotiation must distinguish legacy rendered-context behavior from the new structured contract. Old packages must not silently receive incompatible input semantics. Determine whether additive fields suffice or a supported SDK/manifest version extension is required before implementation; do not reinterpret existing signed manifests.

### Observability architecture review

ADR 0014 deliberately excludes worker identity, leases, heartbeat and queue topology from public Run events. Add a separate reviewed read-only health projection: aggregate availability with an observation timestamp, and owner-scoped execution counts. Publish no worker identity, claim tokens, raw heartbeat records, foreign-owner workload counts or queue position. Leave the existing event response unchanged.

## Implementation sequence

### 0. Establish the baseline

- Record current migration heads and relevant dirty changes.
- Reconcile roadmap/status wording and record a supported SDK/provider/tool matrix.
- Identify qualification cases for the existing configured-model fallback fix, package install/run/rebind/rollback, independent runs and conversations.
- Preserve the existing API → core, Worker → core, and SDK → public-contract dependency direction.

### 1. Freeze contracts and migration design

- Record successor ADRs for the reviewed extensions above.
- Specify typed context, memory proposals/policies, tool declarations/bindings, safe audit projections and compatibility negotiation.
- Allocate Alembic revisions from the actual current local head. Keep local SQLite migrations independent of hosted Marketplace migrations.
- Define failure, revocation, schema-drift, retry and late-result behavior before adding routes.

### 2. Complete package → MCP mediation

- Reuse durable approved catalog synchronization and the existing Stage-D ToolInvocationMediator.
- Let an owner bind each declared package tool alias to one exact owned connection/tool definition. Never match ambiguous bare names across connections.
- Show missing required bindings separately from optional unavailable tools; readiness is not a grant.
- Require explicit per-instance grants. Connecting a server, installing a package or binding a tool grants nothing automatically.
- Pin binding identity and schema evidence at run acceptance. Live disable/revoke/schema drift still denies invocation; a newly granted tool must not become retroactively available to an older run.
- Preserve existing timeout, cancellation, ambiguity, normalized-error and durable audit behavior. Credentials stay in host-owned executors.
- Add thin owner-scoped APIs for binding/grant/revoke/reconfirm and safe invocation-history queries over core services.

### 3. Complete context and model-message delivery

- Deliver current input, selected memory, bounded history, compaction metadata and rendered context from one immutable snapshot.
- Populate `AgentContext.memory` using the selected snapshot items; add item/version/provenance fields only through the reviewed compatible contract.
- Provide SDK helpers for building a model request once from structured context. Legacy packages retain their documented rendered contract.
- Separate policy/package instructions from memory and external data. Agent-provided text cannot override host policy or tool permission checks.
- Translate supported message roles and request settings explicitly in provider adapters. Reject unsupported semantics with a documented error rather than silently flattening or ignoring them. Tool messages need a qualified identity/result contract before being advertised as supported.
- Preserve the selected instance model when an SDK request omits its model.
- Independent runs may use approved memory under the new contract, but never acquire conversation history automatically. Conversational runs use only their conversation's eligible history.
- Retries and recovery reuse the original context; later memory edits affect later submissions only.

### 4. Add controlled memory writing

Proposed per-instance modes:

| Mode | Permanent memory behavior |
|---|---|
| Manual, default | Existing explicit create/edit/promote behavior |
| Review suggestions | Agent candidates are retained as pending suggestions; owner approves or dismisses |
| Automatic private, opt-in | Eligible validated candidates can become private AGENT facts; USER candidates still require review |

- Start with a typed SDK proposal contract. Packages propose facts; they receive no direct database or unrestricted memory-write port.
- For agents that do not emit proposals, provide a separately enabled, bounded host-owned extraction task after eligible completed runs. It uses the ordinary Run/Job path, configured provider mediation and explicit token/time/queue budgets. An extraction run never triggers another extraction run.
- Save durable facts/preferences, not entire transcripts, credentials or tool authorization. Treat inferred content as unverified data and show its origin.
- Set concrete candidate count/content/batch bounds during contract review, within existing memory item/scope caps; do not silently enlarge frozen limits.
- Tie candidates to owner, instance, source run, accepted policy revision and content digest. Use transactional uniqueness to prevent duplicates during retries and recovery.
- Only the authoritative successful result is eligible. Discard late results after cancellation or lost authority. For conversations, derive from the authoritative finalized turn, not a competing successful projection.
- Persist candidate/extraction obligations durably with authoritative completion so a Worker crash cannot lose or duplicate them. Recheck live policy before materializing automatic facts; disabling automation blocks pending automatic writes.
- Memory retention failure, quota exhaustion or extractor failure must not turn an already successful primary agent run into failure. Expose a separate bounded memory outcome.
- Automatic saving adds facts; it does not overwrite user-authored facts or silently resolve contradictory facts. Surface conflicts for review.
- Avoid recreating deleted/rejected facts from the same source through replay. New observations can be reviewed under current policy.
- Save after completed runs, not with a permanent process for each idle agent. Existing schedules provide periodic agent activity. New facts become eligible for future snapshots, subject to bounded retrieval.
- Explain historical retention: deleting active memory does not erase text from old immutable run snapshots/backups.

### 5. Deliver usable product controls

Reuse React Router, TanStack Query, existing authentication and query-cache isolation. Keep the dark/lime visual identity, but consolidate theme tokens, readable tables, spacing, selected states, focus indicators and responsive layouts.

| Surface | Planned controls |
|---|---|
| Shared navigation | Dashboard, Agents, Packages, Marketplace, Conversations, Memory, Connections, Automations, Runtime health |
| Connections | Owned MCP connections, supported transport, refresh/discovery, enable/disable, safe health/error state; explain operator configuration when needed |
| Agent detail | Runs, Context, Tools & permissions, Memory, Settings; declared tools, explicit bindings/grants and drift/reconfirmation |
| Tool history | Owner-scoped invocation status, safe tool identity, run link and sanitized errors; no credentials or raw arbitrary audit payloads |
| Memory | Scope/agent filters, manual creation, suggestions/review, policy control, provenance/version history, edit/delete, automatic-save status |
| Run detail | Exact context used: selected memory versions, bounded messages, trimming information and memory outcome |
| Runtime health | Safe observed availability and owner queue/execution counts; explain stale/unavailable observations without claiming why a run is queued unless evidence supports it |

Forms require server validation, accessible labels/keyboard operation, distinct loading/empty/error states and visible conflict handling. Changes must respect authenticated ownership and unsafe-request origin protection. Existing routes and workflows remain reachable.

### 6. Qualify and document before declaring completion

Planned checks: ownership isolation; manifest/version compatibility; tool-name collisions; connection disable, grant revoke/regrant and schema drift; context delivered exactly once; legacy and new host/SDK combinations; model defaults and unsupported-role behavior; immutable retry snapshots; failed/cancelled/late memory candidates; duplicate completion; memory policy revocation; quota/conflict/deletion behavior; worker restart; concurrent agents; migration upgrade/downgrade; UI accessibility and private cache hygiene.

Use deterministic local fixtures for automated regression checks, then a supervised journey with a context-aware package and controlled MCP server. Demonstrate manual and conversational memory use, suggestion approval, opt-in automatic private saving, permission denial/revocation, and execution recovery. A live-provider check is separate from deterministic tests.

No tests were added or run while preparing this plan. Implementation status changes only after authorized qualification and acceptance evidence; no milestone is marked complete merely because code exists.

## Expected code areas

- `packages/nervos-core`: context assembly/submission, memory services and lifecycle, tool bindings/grants/audit queries, safe health projections and Alembic migrations.
- `packages/nervos-sdk`, `packages/nervos-package-host`: negotiated public context/proposal types and bounded serialization.
- `apps/worker`: package tool/model mediation, authoritative candidate/result handling and ordinary extraction-task execution.
- `packages/nervos-models`: supported structured-message translation and explicit capability/error handling.
- `apps/api`: authenticated schemas/routes and dependency composition over application services.
- `apps/web`: shared layout, Connections/health pages, agent tools/context controls and improved memory UI.
- Documentation: runtime, SDK/package compatibility, memory/tool user guides, successor ADRs and accurate delivered-status updates after acceptance.

## Scope limits

This plan does not deliver Stage-H OAuth account connections, encrypted Secret Manager, interactive external-action approvals, hostile-code sandboxing or complete trust/revocation enforcement. Memory suggestion review is not approval to send email or modify another account. Static MCP credentials remain host-owned under existing policy.

No claim of universal LangChain/LangGraph compatibility, durable arbitrary framework checkpoints, unlimited context, vector search, shared workspace memory or production readiness follows from this work. New SDK adapters still need package dependency admission and qualified public interfaces. Keep the modular runtime and existing Run → Job → Worker execution authority.


## Local implementation and verification

Implemented after explicit approval: Alembic `0015_runtime_integration`; owner-scoped API routes; package manifest and SDK/host extension; Worker model/context/tool mediation; durable owner policy and suggestions; ordinary-run extraction; safe runtime health; dashboard Connections, runtime health, agent permissions/memory controls, suggestion review and per-run context views. Discovered tools and suggestions use bounded keyset pagination. All tool invocation remains behind the Stage-D grant evaluator and mediator.

Local qualification: the focused runtime/API/migration/architecture checks passed, including 15
current runtime and architecture regression cases; the real signed-package Worker test passed both
legacy and structured-MCP modes, including permission revocation. The frontend suite passed 156
tests, including memory-policy and suggestion-review interactions. Frontend typecheck, lint and
production build passed; Ruff, formatting, Pyright (zero diagnostics) and the security scan (718
files, no findings) passed. The full repository Python run executed 3,286 tests: 3,284 passed, and
two test-only failures were identified (a fixture missing its required deleted timestamp and a
Stage-I architecture assertion still pinned to the former schema head). Both corrections are in
place and the affected modules passed all 15 tests on rerun; the full 3,286-test run was not repeated.
This is local implementation evidence, not external or production acceptance. Stage H controls
remain explicitly out of scope.
