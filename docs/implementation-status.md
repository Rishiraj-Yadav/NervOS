# NervOS Implementation Status

## Current phase

**Stage D — the tool and MCP layer — is COMPLETE.** **Stage E — scheduling, events, and triggers —
has begun**: its **E0 architecture freeze** is complete and externally accepted, **E1 — the durable
trigger/occurrence domain and migration `0008` — is complete and accepted**, **E2 — the scheduler
engine — is complete and accepted**, and **E3 — secure webhook ingress, secret authentication,
rotation and idempotency — is complete and accepted.** Stage E decides when a Run comes into
existence, who may cause one, and the single acceptance path every Run travels, in ADRs 0018–0020.

**Schedule and webhook triggers now fire.** A dedicated Scheduler process evaluates one-time,
interval and cron schedules, resolves them in their own IANA timezone under the frozen DST contract,
and turns a due schedule into an **ordinary Run** through the same submission primitive a person
uses. The sibling `POST /hooks/v1/{public_id}` ingress authenticates a Bearer capability secret,
re-checks the current secret transactionally, bounds and canonicalises JSON payloads, applies
Idempotency-Key semantics and turns accepted deliveries into ordinary Runs/Jobs through that same
seam. E3 also provides provider-neutral provisioning and secret-rotation application services.
**E4 is complete and externally accepted.** Internal EventEnvelope publication uses exact owner/event-type matching with bounded fanout; the owner-scoped trigger-management API supports all trigger kinds, schedule management reuses E2, webhook creation and rotation reuse E3, and occurrence history exposes Run provenance. The minimal `/automations` UI supports management and one-time webhook-secret handling. E4 adds no EventRecord, replay, public event HTTP ingress, migration, or dependency. **E5 — integrated acceptance, security, recovery, and documentation closeout — is complete and externally accepted, closing Stage E.**

**Stage F — Conversations, Context, and Memory — has begun.** Its **F0 architecture, protocol,
safety, and lifecycle freeze is complete and externally accepted**, and its authority is
`docs/stage-f/README.md` together with accepted ADRs 0021–0023. **F1 — durable Conversations, Turns,
Messages, and Run linkage — is complete and accepted.** **F2 — ContextBuilder, mandatory
RunContextSnapshot, deterministic compaction, and context bounds — is complete and accepted.**
**F3 — Scoped AGENT/USER Memory, Provenance, Deterministic Bounded Retrieval, and Context Injection —
is complete and accepted.** F3 delivers private USER memory, private AgentInstance-scoped AGENT
memory, explicit direct creation and user-approved promotion, version and provenance foundation
under migration `0011_stage_f3_scoped_memory`, deterministic bounded indexed SQLite retrieval
(≤50 candidates total with keyset refill), active item limits (≤1000 per scope, ≤32000 bytes per
item), memory injection sub-budget (≤4000 bytes), ContextBuilder v2 integration, immutable
memory-bearing snapshots with v1 backward compatibility, single-attempt retry snapshot reuse,
memory/tool authority isolation, and zero automatic memory writes. **F4 — Conversation and memory
lifecycle, versioned editing, deletion, active-data invalidation, retention semantics, and minimal
management UI — is complete and accepted.** F4 delivers memory inspection (`GET /api/v1/memories`),
versioned editing via `expected_version` CAS allocating version N+1, soft deletion (`DELETE
/api/v1/memories/{id}`), active-retrieval invalidation, conversation archive and unarchive (`POST
/archive`, `POST /unarchive`), conversation soft deletion (`DELETE /conversations/{id}`) under
migration `0012_stage_f4_conversation_lifecycle`, non-cascading execution and audit retention,
in-flight run finalization safety, full minimal `/memories` UI, and private query cache hygiene on
logout. **Workspace/shared memory is NOT implemented; FTS/vector/embedding search is NOT
implemented.**

Stage C — Persistent execution engine is COMPLETE. The C0 architecture freeze, the C1 durable execution foundation, C2 — asynchronous submission and minimal durable Worker execution — C3 — Worker registry/health, expired-lease reconciliation, and fencing hardening — C4 — the safe execution retry engine — C5 — owner cancellation and Attempt execution-timeout orchestration — C6 — authoritative global/per-Agent/per-provider execution concurrency, durable Agent fairness, and full admission backpressure — C7 — public read-only execution observability, the Run Events API, the execution timeline, and the polling model — and C8 — integrated deterministic Stage C acceptance and closeout — are implemented, externally reviewed, and accepted.

Stage B — Trusted-agent runtime proof is complete and accepted. B1 domain/persistence, B2 internal one-call execution, B3 trusted Agent/Run HTTP API with the minimal Chat dashboard interaction, and B4 second-provider portability are implemented, merged to `main`, and post-merge verified.

**Stage D — the tool and MCP layer — is COMPLETE.** Its **D0 architecture,
protocol and safety freeze is complete and externally accepted**. D0 delivered governance only: it
froze the tool, capability, permission and tool-durability architecture in ADRs 0015–0017 and changed
no runtime behaviour. **D1 — durable tool, capability, and audit schema is complete and accepted**.
**D2 — capability grants and the call-time permission decision is complete and accepted**.
**D3 — tool registry, canonical schema, and the first built-in tools is complete and accepted**.
**D4 — provider-neutral tool calling and the Think → Act → Observe loop is complete and accepted**.
**D5 — MCP client/gateway and connection lifecycle is complete and accepted**.
**D6 — tool audit, failure semantics, and C3–C6 integration is complete and accepted**.
**D7 — integrated acceptance and Stage-D closeout is complete and accepted**.
The next milestone is **F5 — Integrated acceptance, Stage-F closeout, and documentation**.

## Stage D milestones

- [x] D0 — Architecture, protocol, and safety freeze (documentation/governance only; no schema, no dependency, no implementation)
- [x] D1 — Durable tool, capability, and audit schema
- [x] D2 — Capability grants and the call-time permission decision
- [x] D3 — Tool registry, canonical schema, and the first built-in tools
- [x] D4 — Provider-neutral tool calling and the Think → Act → Observe loop
- [x] D5 — MCP client/gateway and connection lifecycle
- [x] D6 — Tool audit, failure semantics, and C3–C6 integration
- [x] D7 — Integrated acceptance, MVP closeout, and documentation

## Stage E milestones

- [x] E0 — Architecture, protocol, and safety freeze (documentation/governance only; no schema, no dependency, no runtime code)
- [x] E1 — Durable trigger/occurrence domain, migration `0008`, and the shared Run-submission foundation
- [x] E2 — Scheduler: one-time / interval / cron, timezone, misfire, multi-instance, restart
- [x] E3 — Webhook ingress, secret authentication and rotation, idempotency
- [x] E4 — Internal events, trigger management and occurrence history, minimal Automations surface
- [x] E5 — Integrated acceptance and Stage-E closeout

## Stage F milestones

- [x] F0 — Architecture, protocol, safety, and lifecycle freeze (documentation/governance only; no schema, no dependency, no runtime code)
- [x] F1 — Durable Conversations, Turns, Messages, Run linkage, idempotency, assistant finalization, API and minimal UI
- [x] F2 — ContextBuilder, mandatory RunContextSnapshot, deterministic compaction, context bounds
- [x] F3 — AGENT/USER scoped memory, provenance, and deterministic retrieval
- [x] F4 — Conversation and memory lifecycle, delete, and minimal UI
- [ ] F5 — Integrated acceptance and Stage-F closeout

**E0 is architecture frozen and externally accepted.** It delivered governance only and changed no
runtime behaviour. It fixed: that **Stage E decides when a Run exists while Stages C and D continue to
decide how it executes**, so every automatic execution becomes an ordinary Run through the one existing
acceptance seam; the `TriggerDefinition` / `TriggerOccurrence` / Run separation and the two-status
occurrence vocabulary (`run_created`, `skipped`, with duplicates returning the existing occurrence
rather than writing one); the five trigger kinds; one-time, interval and cron semantics including the
frozen 5-field dialect, the UTC storage rule, the IANA-timezone-for-cron rule and the explicit DST
contract; the fixed `COALESCE_ONE` misfire policy; the `next_fire_at` invariants; the dedicated
scheduler process, its fixed polling, and database-arbitrated multi-instance behaviour; the webhook
trust boundary at `/hooks/v1/{public_id}` with a locator separate from the secret and constant-time
verification; the internal event envelope, its owner scope, exact matching and bounded fanout; and
reverse Run provenance through `trigger_occurrences.run_id` with **no column added to `runs`**. It
added no migration, no dependency, no API surface, no frontend, and no source change. Its authority is
**ADR 0018**, **ADR 0019** and **ADR 0020**.

**E1 is complete and externally accepted.** It made a trigger a durable, owner-scoped thing NervOS can
represent, and it made the *act of turning one into a Run* atomic — while still letting nothing fire
one. Migration `0008_stage_e1_trigger_scheduling` adds exactly two tables, `trigger_definitions` and
`trigger_occurrences`, and touches no existing table: all five kinds (one-time, interval, cron, webhook,
event) share one shape whose per-kind fields are forced absent or present by an exhaustive five-branch
`kind_shape` CHECK, so a malformed combination is unrepresentable rather than merely rejected in code.
The occurrence vocabulary is exactly two statuses — `run_created` and `skipped`. There is deliberately
no `duplicate` status: a second materialization of a deterministic occurrence returns the existing
occurrence and writes nothing, because recording a duplicate as a durable status would assert a second
fact about one event. **Run origin is reverse provenance.** `trigger_occurrences.run_id` is a unique
foreign key into `runs` with `ON DELETE RESTRICT`, so **`runs` gained no column at all** and manual
origin is simply the absence of an explaining occurrence — which is why the D1/D2/D4 cutoff, retry,
cancellation and tool-audit code paths needed no change.
**The seam is shared, not duplicated.** E1 promoted the canonical Run+Job insertion out of the
submission method into one module-level `insert_run_and_job_on_connection`, and a trigger materialization
now calls exactly that function: an occurrence, a Run, a Job, the `run.created`/`run.queued` events and
the trigger's own state transition commit in **one** `BEGIN IMMEDIATE`, so a crash before commit leaves
none of them and a crash after leaves all of them. There is no trigger queue, no trigger job type, no
trigger status on `runs`, and no second execution path — a triggered Run is structurally identical to a
manually submitted one and is executed later by the ordinary Worker through Stages C and D unchanged.
The command carries no authoritative trigger fields: owner, target, revision and input are re-read from
the definition inside the transaction, and limits are resolved through the same Agent-Definition resolver
a manual submission uses, so a retargeted trigger fails closed as a `skipped` occurrence instead of
running the wrong Agent. An Agent-disabled target is a `skipped` occurrence, while exceeding queue
capacity rolls the **whole** materialization back — no occurrence, no Run, trigger still due — because
consuming a nominal occurrence to record "we were busy" would silently destroy scheduled work.
**Webhook secret primitives were built but not wired.** `generate_public_id` (exactly 22 URL-safe
characters), `generate_secret` (43), `digest_secret` (SHA-256, 32 bytes) and a fixed dummy digest for
E3's uniform-comparison path exist in `infrastructure/security/webhook_secrets.py`; plaintext is never
persisted, the digest is excluded from `repr`, and no HTTP route references any of it. The frozen
5-field cron dialect is validated by a NervOS-owned validator — not a scheduler library — that rejects
seconds, `L`, `W`, `#`, and every other extension by name, and IANA timezone names are validated in the
domain. The schema-revision guard moved into `nervos-core` and is now shared, with each process
declaring its own expected revision. **E1 added one dependency, `tzdata`**, so the UTC/timezone contract
behaves identically on Windows and in slim containers rather than depending on the host's zone database;
the first run of a timezone test failed on Windows without it, which is exactly the asymmetry it closes.
**E1 added no scheduler, no due scan, no cron evaluation, no next-fire calculation, no REST API and no
frontend.** Its authority remains ADRs 0018–0020, which it did not amend.

**E2 is complete and externally accepted.** It is the milestone that makes a trigger *fire*. A
dedicated **`apps/scheduler`** process — separate from the Worker, holding no provider credential and
opening no remote connection — polls on a **fixed five-second interval** with no idle backoff, reads a
**bounded page of at most 32 due schedules** in a stable `(next_fire_at, id)` order, and hands one
decision at a time to a transaction that verifies it against durable state before applying it.

**All three schedule kinds are evaluated.** A one-time schedule fires once for the instant it names
and completes. An interval schedule advances on an exact fixed-duration sequence, and its misfire
arithmetic is O(1) — a three-day outage on a five-minute interval resolves in one division and produces
**exactly one** catch-up occurrence, not 864. A cron schedule is evaluated through a single `cronsim`
adapter confined to one infrastructure module, on the zone's **local wall clock**, with every
local-to-UTC conversion performed by NervOS in the domain. `COALESCE_ONE` therefore holds for both
recurring kinds: the occurrence is the **latest missed** nominal instant, and the next fire time is the
first strictly-future one.

**The DST contract is met by NervOS, not by the library.** A local time that does not exist fires once
at the **gap-end transition instant** — for `America/New_York`, `30 2 * * *` on 2026-03-08 resolves to
`07:00Z`, which is neither `fold` value `zoneinfo` offers, so the transition is located by a bounded
bisection over a window exactly one gap wide. A local time that happens twice fires once, on the first
occurrence, and the second pass is suppressed structurally because iteration happens in wall-clock
space. Non-hour shifts are handled — `Australia/Lord_Howe`'s thirty-minute transition included — and
the tests assert NervOS semantics while separately pinning what the library itself returned, so an
upstream change fails loudly rather than silently altering scheduling behaviour.

**Materialization is atomic and duplicate-safe.** One `BEGIN IMMEDIATE` contains the trigger re-read,
the existing-identity lookup, the precondition checks, the canonical Run + Job insertion, the
occurrence insert, and the schedule state transition. **Identity is resolved before authority**: a
decision naming an occurrence that already exists returns that occurrence and writes nothing, whatever
has since happened to `enabled`, `config_revision` or `next_fire_at` — which is what stops a second
scheduler from reporting a failure for work that already succeeded, and what makes a one-time trigger,
which disables itself as it fires, safe to race. Authority is still applied in full to every **new**
identity. Two schedulers need no leader election, no lease and no coordination table: SQLite's write
lock decides which proceeds, and the database's own facts decide what the loser does — a duplicate
identity, or a **stale** decision refused because the revision or the next fire time moved. A
process-local keyset c‍ursor keeps a page blocked by admission backpressure from stranding later due
schedules; it is fairness state only, has no durable representation, and correctness never depends on
it.

**The Scheduler decides *when*, never *how*.** It never calls a model, executes a tool, speaks MCP,
claims a Job, creates an Attempt, terminalizes or retries a Run, or bypasses D2. A Run it creates is an
ordinary Run with the same Agent snapshot, the same grant-cutoff snapshot, the same admission control
and the same queue as a manual submission — asserted structurally, and enforced by architecture guards
that forbid the scheduler from importing any execution module. **Queue-capacity backpressure rolls the
whole candidate back** and leaves the schedule due rather than recording a skip; an Agent that is
disabled yields a `skipped` occurrence and does not end a recurring schedule; a schedule that can no
longer be evaluated records one skip with a static reason and retires itself. Errors are isolated per
trigger, while schema, database and integrity failures stop the process rather than being swallowed.

**E2 added no migration** — the head remains `0008_stage_e1_trigger_scheduling`, and the trigger
tables, the partial due index and the `next_fire_alignment` invariant were already sufficient. It added
**one dependency, `cronsim>=2.7,<3`**, as a calculation library with no scheduler framework behind it.
**E2 added no webhook or event ingress, no trigger management or occurrence-history API, and no
frontend surface.** Its authority remains ADRs 0018–0020, which it did not amend.

**E3 is complete and externally accepted.** The sibling `POST /hooks/v1/{public_id}` ingress uses a
Bearer capability secret whose SHA-256 digest is compared in constant time and re-checked against the
current durable secret inside the materialization transaction. Raw bodies are streamed under the
frozen 65,536-byte bound before parsing; accepted top-level JSON objects are validated and rendered
canonically as untrusted data in an ordinary Run input envelope. Optional `Idempotency-Key` values
provide durable same-key deduplication, with a same-key/different-body conflict and distinct keyless
deliveries. Accepted deliveries use the shared atomic TriggerOccurrence → ordinary Run/Job seam;
Agent-disabled deliveries become static skips, queue capacity rolls back for sender retry, and
Agent-definition drift consumes nothing. Provider-neutral provisioning and secret-rotation services
return plaintext credentials only once; rotation leaves the public locator stable and does not change
`config_revision`. The ingress adds no migration, queue, Run Event, raw-payload table, management API
or frontend surface. The developer default database was accidentally migrated from `0007` to `0008`
during an implementation smoke command; no data was lost, it was not downgraded or otherwise touched
during finalization, and the deviation remains recorded in the E3 implementation report.

**E4 is complete and externally accepted.** Internal events are published through a provider-neutral
`EventEnvelope` **value** — an owner, an event type, a canonicalized payload and an instant — and
never through an HTTP ingress: an event is data, not authority, and no public route can create one.
Publication discovers the owner's currently-enabled event triggers by **exact** event-type equality
(no wildcard, no substring, no case folding), reads **one row past** the frozen fanout bound of 32 so
an over-wide event is provable, and refuses the whole publication before any write when the bound is
exceeded. Each candidate then materializes in its own short transaction through the same
`TriggerOccurrence` → ordinary Run/Job seam the scheduler and the webhook ingress already use, so an
event-created Run is an ordinary Run with the same Agent snapshot, grant cutoff, admission control
and queue as a manual submission.

**Per-candidate isolation is the shape of a publication.** Queue-capacity backpressure rolls back only
its own candidate and leaves that identity unconsumed and retryable while independent candidates
proceed; an Agent-disabled target consumes the identity as a static `skipped` occurrence; a target
whose Agent Definition drifted between discovery and materialization consumes nothing. A
database-level failure is not a per-trigger outcome — it stops the publication, keeps what already
committed, and reports `complete = false` so the caller may retry the same envelope. **Event identity
is the event id, arbitrated before authority:** a repeat of the same id with the same payload answers
`duplicate` and writes nothing, the same id with a different payload is a caller-contract
`event_id_conflict`, and a retry after a partial fanout fills only the missing identities. **There is
no event ledger** — an event exists durably only as the per-trigger occurrences it produced, so there
is no replay table, no raw-payload store and no global event history.

**The owner-scoped management surface manages every kind.** One trigger resource family lists, reads,
creates, edits, enables, disables and deletes owner-scoped triggers of all five kinds, paginated by a
keyset cursor rather than an offset, with a foreign or nonexistent identifier indistinguishable from
a missing one. An edit that would rewrite a trigger's kind-shaped identity, and a delete of a trigger
that already has history, are refused rather than silently destructive. Occurrence history exposes
each occurrence's Run provenance, which is a join rather than a column on `runs`. The minimal
`/automations` surface manages triggers and presents a webhook's secret exactly once, at creation or
rotation: plaintext never enters mutation state, browser storage or a later read.

**E4 added no migration, no index and no dependency.** `0008` already carried every access path the
management surface and the event seam need, and the six hot-path reads — the owner list and its keyset
page, the owner-scoped detail read, one trigger's occurrence history, exact event discovery, the
event-identity lookup and the webhook locator lookup — are each proven, by planning the statement the
composed method actually ran against a populated database, to be an index seek over `0008`'s own
indexes rather than a scan of a table that grows with the owner's data. E4 added **no EventRecord, no
replay, no public event ingress and no second execution seam**. It did add provider-neutral
application ports — an exact-match event-discovery read, an event-materialization write and the
owner-scoped management operations — but each is a `Protocol` over values implemented by the
persistence adapter: no SQLAlchemy type, session or ORM row crosses into the application layer, and a
trigger-created Run travels the same canonical insertion as a manual one. Its authority remains
ADRs 0018–0020, which it did not amend.

D0 is **architecture frozen and externally accepted**. It fixed: the MCP protocol target
(`2026-07-28`, modern era only, Streamable HTTP and stdio, official SDK v2 with no custom protocol
stack); the capability model (per-Agent Instance grants held as explicit ALLOW rows, default not
granted, evaluated live before every call); the durable monotonic Run grant cutoff that makes
revocation immediate and a new grant invisible to a Run already started; the two-transaction
call boundary with its ten re-checked predicates; the definition fingerprint and drift rule; the
no-blind-replay rule and the crash matrix that follows from it; and the C4 narrowing that forbids a
whole-Attempt retry once any tool has dispatched. The boundary with Stage H is explicit: Stage D owns
static grants and call-time enforcement, while Stage H owns interactive approvals, encrypted secret
management, sandboxing and deeper isolation.

**D0 shipped no user-facing capability.** There is no `0007` migration, no tool table, no MCP
dependency in the lockfile, no tool registry, no execution loop, and no API or UI surface for tools.
The migration head remains `0006_stage_c6_queue_partitions`. D1 will implement the reviewed schema
separately.

## Stage C milestones

- [x] C0 — Durable execution architecture freeze (documentation/governance only; no schema, no implementation)
- [x] C1 — Durable execution foundation (dormant Job/Attempt/RunEvent domain, `0003` migration, schema-parity protection)
- [x] C2 — Asynchronous submission and minimal durable Worker execution
- [x] C3 — Worker registry/health, expired-lease reconciliation, and fencing hardening
- [x] C4 — Safe execution retry engine (durable `SAFE_TO_RETRY` re-execution and backoff)
- [x] C5 — Owner cancellation and Attempt execution-timeout orchestration
- [x] C6 — Authoritative execution concurrency, durable Agent fairness, and admission backpressure
- [x] C7 — Public read-only execution observability, Run Events API, execution timeline, and polling model
- [x] C8 — Integrated deterministic Stage C acceptance and closeout

C2 passed external source review after a bounded remediation pass, passed every hosted check on pull
request [#11](https://github.com/Rishiraj-Yadav/NervOS/pull/11), and was merged to `main` in merge
commit `21a588452b5a01f5bd099d21ccaacd7f7584b483`. The long-lived `stage-c` branch was
fast-forwarded to that merged state.

C3 passed external source review of its plan, then external implementation review, then a dedicated
E2E acceptance remediation that added the deterministic pre-start recovery journey. C4 passed
external review of its plan, then external implementation review. C4 is authored and verified
together with this status update in a single C4 milestone change, so it deliberately records no pull
request number and no merge commit of its own. C5 passed external review of its plan, then external
implementation review, then a remediation pass that completed the outer execution-timeout watchdog
and the bounded Worker shutdown drain. C5 is likewise authored and verified together with this status
update in a single C5 milestone change, so it records no pull request number and no merge commit of
its own. C6 passed external review of its plan — which rejected the planned global-Attempt-cursor
fairness algorithm on evidence — then external implementation review. C6 is likewise authored and
verified together with this status update in a single C6 milestone change, so it records no pull
request number and no merge commit of its own. C7 and C8 were planned together as one final Stage C
delivery, passed external review of that combined plan — which corrected the terminal-catch-up
contract and restated the security boundary as a writer-side guarantee — then external review of the
combined implementation, which accepted both. C7 and C8 are likewise authored and verified together
with this status update in a single Stage C closeout change, so they record no pull request number
and no merge commit of their own.

## Stage B milestones

- [x] B0 — Scope freeze and first trusted-agent architecture
- [x] B1 — Agent-instance and run domain/persistence
- [x] B2 — Model adapter, process-secret foundation, and bounded proof runner
- [x] B3 — Trusted Chat Agent API and minimal dashboard interaction
- [x] B4 — Second-provider portability and Stage B final acceptance

B0 is a documentation/governance milestone. ADR 0007 freezes a one-shot trusted `nervos.chat` definition identified by exact key/version, explicit user-owned instances, immutable-snapshot Runs, a narrow application-owned model port, process-only provider credentials, one bounded model call, the `created -> running -> succeeded|failed` lifecycle, and an awaited API-process proof runner. B1 implements the domain/persistence foundation, B2 implements the internal Anthropic execution path, B3 exposes that same path over an authenticated, owner-scoped HTTP API with a minimal trusted Chat UI, and B4 adds OpenAI Responses as a second production adapter behind the unchanged port. ADR 0008 records the B4 portability decision.

## Stage A milestones

- [x] A0 — Stage A plan reviewed
- [x] A1 — Repository and tooling bootstrap
- [x] A2 — FastAPI + typed configuration + SQLite + SQLAlchemy + Alembic
- [x] A3 — First-run setup + local authentication
- [x] A4 — React dashboard foundation
- [x] A5 — Stage A E2E flow
- [x] A6 — CI, tracked-file security scanning, and Stage A documentation completion
- [x] A7 — Final architecture/security/test audit

## Stage A acceptance criteria

- [x] clean checkout can bootstrap dependencies
- [x] API starts successfully
- [x] `/api/v1/health` succeeds
- [x] migrations work from empty database
- [x] first user can initialize NervOS exactly once
- [x] user can log in
- [x] authenticated session survives browser refresh
- [x] logout invalidates server-side session
- [x] protected dashboard is unavailable unauthenticated
- [x] backend tests pass
- [x] frontend tests pass
- [x] E2E smoke flow passes
- [x] Ruff passes
- [x] Pyright passes
- [x] frontend TypeScript check passes
- [x] production frontend build passes
- [x] CI matches local quality commands
- [x] no real secrets are committed

## Not implemented in Stage A

- AgentPackage/AgentInstance execution
- retry/recovery worker engine beyond the minimal C2 durable Worker
- scheduler/event router
- model providers
- MCP/tool gateway
- agent memory
- agent conversation sessions
- package installer
- marketplace
- IoT
- multi-agent orchestration
- user management

## A1 verified checks

- `uv sync --frozen --all-packages`
- `uv run python scripts/check.py check`
- Ruff check and format check
- Pyright strict type checking
- Pytest tooling suite (6 tests)
- ESLint and TypeScript checks
- Vitest foundation (`--passWithNoTests`; product tests begin in A4)
- Workspace membership, ignored-file, syntax, lockfile, and no-product-behavior audits

## A2 verified checks

- `uv sync --frozen --all-packages`
- `uv lock --check`
- `uv run python scripts/check.py lint`
- `uv run python scripts/check.py typecheck`
- `uv run python scripts/check.py test`
- `uv run python scripts/check.py check`
- Pytest backend/tooling suite (51 tests)
- Isolated Alembic upgrade, current/head, no-drift check, downgrade, and re-upgrade
- Migration-first development launcher and exact `GET /api/v1/health` smoke check
- Ruff, Ruff format, Pyright strict, ESLint, TypeScript, and Vitest foundation checks

## A3 verified checks

- `uv sync --frozen --all-packages`
- `uv lock --check`
- `uv run python scripts/check.py lint`
- `uv run python scripts/check.py typecheck`
- `uv run python scripts/check.py test`
- `uv run python scripts/check.py check`
- Pytest backend/tooling suite (100 tests after final handoff remediation)
- Real file-backed setup race exercised 20 times with exactly one user/session
- Manual setup/login/me/logout/replay/login smoke flow over real Uvicorn
- Public setup-status discovery, JSON-only credentials, exact-origin unsafe-API enforcement, received-body limits, and response security-header checks
- Alembic still head `0001_stage_a` with no schema drift
- Dedicated security/test/architecture reviewers: no CRITICAL/HIGH findings; concurrency bound, safe 503 mapping, pre-body Origin/size boundary, setup-complete precheck, and single-owner policy fixes applied

## A4 verified checks

- `pnpm lint`
- `pnpm typecheck`
- `pnpm test` (38 Vitest/Testing Library/MSW tests)
- `pnpm build`
- `uv run pytest tests/test_tooling.py apps/api/tests/integration/test_setup.py apps/api/tests/integration/test_authentication.py apps/api/tests/integration/test_auth_security.py apps/api/tests/integration/test_health.py` (25 passed)
- `uv run python scripts/check.py check` (101 Python tests and 38 frontend tests passed)
- `git diff --check`
- Isolated temporary-database browser smoke: setup, canonical dashboard identity, logout, login, refresh restoration, dashboard, and final logout
- Focused architecture and security reviews completed; wildcard routing and abort-classification findings fixed

## A5 verified checks

- `uv lock --check`
- `pnpm install --frozen-lockfile`
- `pnpm --dir apps/web exec playwright install chromium` (Chromium only)
- `uv run pytest tests/test_e2e.py tests/test_tooling.py` (17 focused supervisor/tooling tests)
- `pnpm --dir apps/web lint`
- `pnpm --dir apps/web typecheck`
- `pnpm --dir apps/web test` (38 Vitest/Testing Library/MSW tests)
- `pnpm --dir apps/web build`
- `uv run python scripts/check.py check` (110 Python tests and 38 frontend tests; E2E remains intentionally separate)
- `uv run python scripts/check.py e2e` twice consecutively, each with a newly Alembic-migrated temporary SQLite database
- Controlled Playwright timeout path, proving supervisor cleanup of only owned process trees
- `git diff --check`

## A6 verified checks

- `uv lock --check` and `uv sync --frozen --all-packages`
- `pnpm install --frozen-lockfile`
- `uv run python scripts/bootstrap.py --skip-browser` (dependency-only path; Chromium deliberately not provisioned)
- `uv run python scripts/bootstrap.py` (A5 default path unchanged; Chromium still provisioned)
- `uv run python scripts/check.py check` (164 Python tests and 38 frontend tests, now including the tracked-file security scan)
- `uv run pytest tests/test_security_scan.py` (36 scanner tests: positive and negative fixtures, placeholder suppression, allow-marker scoping, and a sentinel assertion proving no matched value reaches stdout or stderr)
- `uv run pytest tests/test_clean_check.py` (6 tests, including proof that the export never mutates the active checkout and needs no Git author identity)
- `uv run pytest tests/test_tooling.py` (21 tooling tests, including workflow invariant checks)
- `uv run python scripts/security_scan.py` and `--tracked-only` (176 and 166 files scanned, no findings in either mode)
- `pnpm build`
- `uv run python scripts/check.py e2e` twice consecutively, each with a newly Alembic-migrated temporary SQLite database
- `uv run python scripts/clean_check.py` — all five steps PASS inside an isolated export (lockfile, bootstrap, check, build, e2e) with exit code 0
- Ruff, Ruff format check, Pyright strict, ESLint, and strict TypeScript
- `.github/workflows/ci.yml` and `security.yml` parse as valid YAML, declare `permissions: contents: read`, reference no secrets, and pin every action to a verified full-length commit SHA
- `git diff --check`

### GitHub-hosted validation

Both workflows were then exercised on real GitHub-hosted runners through pull request [#1](https://github.com/Rishiraj-Yadav/NervOS/pull/1) (`stage-a-a6-ci-validation` → `main`), triggered by `pull_request`, at commit `760c4e8`:

- CI — run [34699167313](https://github.com/Rishiraj-Yadav/NervOS/actions/runs/34699167313), attempt 1, `success`
  - `Repository checks` (`check` job): every step succeeded — checkout, pnpm, Node from `.node-version`, uv, `uv python install`, `uv lock --check`, `bootstrap.py --skip-browser`, then `check.py check` — with no Chromium provisioning
  - `Deterministic browser journey` (`e2e` job): every step succeeded — full bootstrap with the project-local Chromium, then the CI-only `playwright install-deps chromium`, then `check.py e2e`
- Security — run [34699167312](https://github.com/Rishiraj-Yadav/NervOS/actions/runs/34699167312), attempt 1, `success`
  - `Tracked-file security scan`: the repository-owned scanner ran on the runner's `actions/setup-python` interpreter and reported no findings

Both runs uploaded zero artifacts and required no retry (attempt 1). The only annotation was a non-failing `astral-sh/setup-uv` cache-reservation warning on the `e2e` job, caused by the two CI jobs starting in parallel and contending for one cache key; the job succeeded and that cache is only an optimization. Workflow job logs are not retrievable without repository credentials, so this record rests on per-step run results rather than raw log text.

## A7 final verification

Pull request [#1](https://github.com/Rishiraj-Yadav/NervOS/pull/1) is merged. Its merged `main` commit is `a76c931e618977d1f6bfe992c22173989d52f4d2`; the merged tree was audited as the final Stage A candidate. The real GitHub-hosted merge-commit validation succeeded on its first attempt: CI run [34699701274](https://github.com/Rishiraj-Yadav/NervOS/actions/runs/34699701274) passed both `Repository checks` and `Deterministic browser journey`, and Security run [34699701202](https://github.com/Rishiraj-Yadav/NervOS/actions/runs/34699701202) passed `Tracked-file security scan`.

A7 remediation added safe persistence-failure HTTP regression coverage for setup, login, current-user resolution, and logout; direct E2E-supervisor regression coverage for default-database rejection and post-run mutation detection; clean-check failure-path coverage for stop-on-first-failure, nonzero status, temporary-export cleanup, unchanged active checkout, and no Git-author requirement; and concise tracked backend/frontend reviewer definitions scoped only to current Stage A review.

Final local verification passed on the uncommitted A7 remediation candidate:

- `uv lock --check`
- architecture tests (4 passed), core unit tests (36 passed), core integration tests (10 passed), API tests (47 passed), tooling/scanner/clean-check/E2E-supervisor tests (75 passed), and the complete Python suite (172 passed)
- ESLint, Pyright strict, frontend TypeScript, Vitest (38 passed), and production frontend build
- local and tracked-only repository-owned security scans (176 files each, no findings)
- isolated Alembic upgrade/current/check/downgrade/re-upgrade with application schema `users` and `auth_sessions`
- `scripts/check.py` lint, typecheck, test, security, and aggregate check gates
- two consecutive deterministic E2E runs, each with a new temporary Alembic-migrated SQLite database
- `scripts/clean_check.py` in a Git-selected isolated export, including its E2E step

No normal NervOS database was created or used. No unresolved blocking finding remains. The A7 documentation, governance, and regression-coverage findings are resolved. No release, tag, deployment, push, or Stage B work occurred.

## Accepted Stage A limitations

- The repository-owned scanner is bounded Git-selected secret hygiene, not a comprehensive dependency audit or SAST; non-SQLite binary and text files larger than 1 MiB do not receive content-rule scanning.
- The Argon2 password-work bound is process-wide rather than host-wide.
- Without an operator bootstrap secret, first-run setup requires a trusted interface; non-loopback deployments need their own network/proxy controls.
- Non-credential endpoints have no global request-body cap.
- Expired or revoked session rows are filtered but not automatically pruned.
- CSRF protection uses exact-Origin enforcement and `SameSite=Lax`, not a separate anti-CSRF token.
- SQLite WAL is deliberately disabled for the local Stage A model.
- Ignored historical Claude worktrees are local housekeeping and are not repository content or acceptance evidence.
- Anonymous GitHub metadata does not expose raw runner logs; run, job, and step metadata supplied hosted validation evidence.

## B0 architecture verification

B0 was implemented as documentation/governance only. ADR 0007 and the Stage B roadmap/runtime documentation define the reviewed B0–B4 boundaries, exact-version trusted definition identity, explicit non-unique user instances, one-shot Runs, three failure classes, immutable execution snapshots, proof limits, process-only provider secrets, and the accepted created/running stranded-state limitation. Backend/frontend reviewer definitions now review only behavior accepted in the current milestone while continuing to flag later-stage scope expansion.

Verification included focused governance tests, both repository security-scan modes, `git diff --check`, the canonical `scripts/check.py check` gate, and the full isolated `scripts/clean_check.py` gate. No source, runtime, migration, route, frontend product UI, provider dependency, secret storage, queue/worker, tool, memory, scheduler, package, SDK, or Marketplace behavior was added.

## B1 implementation verification

B1 implements the exact-version built-in definition resolver, explicit owner-scoped Agent Instances, immutable-snapshot Runs, four-state conditional persistence, the additive `0002_stage_b1_agent_instances_runs` migration, and the narrow provider-neutral model completion contract. It adds no provider, execution coordinator, API, UI, Job, tool, memory, package, or secret behavior.

Verification includes focused domain and persistence tests, migration upgrade/current/check/downgrade/re-upgrade on disposable databases, the canonical repository check, both security scanner modes, two deterministic E2E regressions, and isolated clean-check. `FIRST PROVIDER DECISION REQUIRED BEFORE B2`.

## B2 implementation verification

B2 selects Anthropic as the first provider, activates `packages/nervos-models`, and uses the official async Anthropic Messages API behind the unchanged B1 model port. `ANTHROPIC_API_KEY` is optional process-only secret-aware configuration; `.env` remains unloaded and API startup remains credential/network independent. The trusted `nervos.chat@1` behavior makes one completion/`messages.create` invocation with SDK retries disabled, applies the immutable Run timeout both to the SDK request and an outer async deadline, excludes thinking/redacted-thinking, validates exact bounded text, keeps `total_tokens` NULL, and persists terminal success/failure only after a transaction-free provider await.

Automatic verification uses deterministic fakes and temporary SQLite only. Focused adapter, handler, provider-registry, coordinator, configuration/startup, architecture, B1 persistence, and migration regressions pass, followed by full Python/frontend, scanner, canonical, two-E2E, and clean-check gates. The default NervOS DB remains untouched.

`REAL PROVIDER PROOF NOT EXECUTED — CREDENTIAL/ACCESS UNAVAILABLE`

## B3 implementation verification

B3 exposes the already-proven B2 execution path as authenticated HTTP resources plus a minimal trusted Chat dashboard interaction. It adds no schema change (head remains `0002`), no migration, no new dependency, no second execution path, and no new execution capability.

Implemented: owner-scoped `GET/POST /api/v1/agent-instances`, `GET/PATCH /api/v1/agent-instances/{id}`, `POST/GET /api/v1/agent-instances/{id}/runs`, and `GET /api/v1/runs/{id}`; four thin `AgentService` read pass-throughs; a dedicated Agent/Run error map; an explicit per-route content-type/body-size rule layered onto the existing credentialed-route controls without changing them; a `Cache-Control: no-store` default on versioned API responses; the `/agents` and `/agents/:agentInstanceId` pages; and the `RunItem` persisted-Run card.

A foreign id and a nonexistent id are indistinguishable: the instance `GET`/`PATCH`, the run `GET`, the instance's run-creation `POST`, and the instance's run-list `GET` all return the identical 404 body. The nested run list resolves the owned parent before reading any Run, so an owned instance with no Runs is a `200` empty page rather than a not-found, and a foreign instance's history is a `404` rather than a misleading empty page.

Frozen contract properties: the B3 API permits creation only for the exact `nervos.chat@1` pair; an unknown provider is rejected on creation **and** configuration update while a known-but-unconfigured provider is accepted at configuration time; a `PATCH` is exactly one of two shapes and performs exactly one committed application write; `usage` is null exactly when nothing trustworthy was persisted and `total_tokens` is never derived; a persisted `failed` Run is returned as 201 with the Run body, never as a 5xx; and `POST …/runs` is the only route that creates a Run.

One user submission is one independent Run. The fixed instruction plus the current text is all that is sent; no prior Run's input or output ever becomes model context, and there is no `conversation_id`, `session_id`, or Message record. This is proven by a test asserting that a second Run's observed model request contains only the second input.

Verification: focused application, API, architecture, and frontend tests; migration upgrade/current/check/downgrade/re-upgrade on a disposable database confirming exactly two revisions and no schema drift; the full Python suite; frontend lint/typecheck/test/build; both security-scanner modes; the canonical `scripts/check.py check` gate; two consecutive deterministic E2E journeys, each on its own fresh temporary database; and the isolated `scripts/clean_check.py` gate. The default NervOS database remained absent throughout.

The browser journey runs against a real API subprocess whose supervisor launches a test-only ASGI factory outside the shipped packages and explicitly removes `ANTHROPIC_API_KEY` from the child environment, so automated E2E cannot consume a real credential. Production composition (`nervos_api.main:app`) and `scripts/dev.py` are unchanged.

`REAL PROVIDER PROOF NOT EXECUTED — CREDENTIAL/ACCESS UNAVAILABLE`

## B4 implementation verification

B4 adds a second production provider without changing the core port, the B3 API surface, or the schema. It adds no migration (head remains `0002`), no new REST route (the OpenAPI surface remains exactly the seven B3 operations), and no provider-status endpoint.

Implemented: canonical provider `openai` through the official asynchronous `openai` SDK and the stateless Responses API, alongside the unchanged `anthropic` adapter; one optional process-only `OPENAI_API_KEY` setting with blank-to-unavailable normalization and no `.env` loading; a composition that knows exactly both canonical IDs and constructs each client only when its credential exists, with `max_retries=0` and explicit async close; a static two-option provider selector shared by the Agent create and configuration pages; and provider/model display on each Run card.

Frozen OpenAI contract: exactly one awaited `responses.create` per execution with `store=False`, `background=False`, and `stream=False`, and no conversation, previous-response, tools, metadata, temperature, or reasoning controls. Output is normalized by inspecting typed `response.output` directly: recognized reasoning items are ignored, at most one assistant message is accepted, ordered `output_text` parts are concatenated with no separator or normalization, any refusal dominates and is never exposed, and tool/unknown output items fail closed. Accepted visible text is required only for a completion, so an empty or blank `completed` response is still invalid; a `max_output_tokens` or `content_filter` outcome remains canonical even when the response carries no output item at all, and any partial text accompanying it is discarded rather than exposed. `completed` maps to `stop`; `incomplete:max_output_tokens` to `model_output_incomplete`; refusal and `incomplete:content_filter` to `model_refused`; `incomplete:max_messages`, `incomplete:steered`, unknown or absent incomplete reasons, and `in_progress`/`queued`/`cancelled`/unknown statuses to `model_response_invalid`; `failed` maps conservatively to `model_unavailable`. A non-null structured `response.error` is never serialized. The immutable Run `model_name` snapshot remains the operator's opaque string and is never rewritten by a provider-returned alias.

Usage: OpenAI retains provider-reported input, output, and total counts and never derives them; Anthropic's `total_tokens` remains `NULL` for the same reason. Cache, reasoning, and tool breakdowns are discarded. Typed SDK failures map most-specific-first onto the existing NervOS codes with no string parsing, and external cancellation still propagates.

Pre-acceptance remediation: an earlier adapter revision decided its outcome and then unconditionally demanded visible text, so an empty `incomplete:max_output_tokens` or `incomplete:content_filter` response was misclassified as `model_response_invalid` instead of `model_output_incomplete` or `model_refused`. Normalization order is now structured provider error, then unrecognized status, then any unacceptable output item, then an explicit refusal, and only last the presence of accepted visible text; item validation still applies to every outcome, so a tool or unknown item fails closed even alongside a recognized non-success status. Composition no longer leaves a secret-bearing settings object reachable: each credential is read once locally to build its client, middleware and `app.state` then receive a credential-free copy, credential fields are excluded from repr and serialization, and app-state, log, and OpenAPI reachability are asserted by test. Both production client factories now pin their canonical HTTPS API base URL and additionally own their authentication and scoping headers, so the ambient `OPENAI_BASE_URL`/`ANTHROPIC_BASE_URL`, `OPENAI_CUSTOM_HEADERS`/`ANTHROPIC_CUSTOM_HEADERS`, `ANTHROPIC_AUTH_TOKEN`, and `OPENAI_ORG_ID`/`OPENAI_PROJECT_ID` variables can no longer redirect a canonical provider, replace its authentication, or scope its request. Each factory then replaces the SDK custom-header layer through public `with_options(set_default_headers=...)`, restoring canonical HTTP basics from frozen/public SDK values, the public platform-header family, required protocol headers, and NervOS-owned authentication/scoping policy while applying the public `Omit` sentinel to every ambient-only name. Therefore neither an arbitrary new header nor a hostile value colliding with an SDK-owned canonical name can reach the prepared request. Stage B supports exactly one provider-environment input per provider, its API key. The isolation uses only public SDK mechanisms; the exact boundary is recorded in `docs/runtime.md`.

Verification uses deterministic doubles for both providers only. The E2E supervisor removes both `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` from child environments, installs two distinct provider-identifying offline fakes with fail-closed startup assertions, and runs the same instance with Anthropic, then OpenAI, proving both immutable snapshots survive reload. Cross-provider tests assert one call, disabled SDK retries, the shared canonical finish vocabulary, safe errors, and cancellation propagation for both adapters. A dedicated no-fallback regression proves a failing selected provider makes exactly one call and never invokes the other.

```text
ANTHROPIC LIVE PROOF — NOT EXECUTED
OPENAI LIVE PROOF — NOT EXECUTED
```

## C5 implementation verification

C5 adds the two execution controls the engine was missing: an owner can stop a Run, and an Attempt
can no longer hold a Worker slot forever.

**Cancellation is a durable control-plane transition, not a message to a Worker.** The API
transaction that accepts the cancellation *is* the cancellation: it records the request, closes the
Attempt, Job, and Run, and appends the cancellation events atomically before the response returns.
A live Worker is therefore never required — cancellation completes with no Worker running, with a
crashed Worker, and against a Job that is merely queued or waiting out a scheduled retry.
`cancel_requested_at` is write-once and is never cleared; because the request and the Job's terminal
state commit together, a Job carrying a request can never be claimable, which is why the claim query
needed no new predicate. Repeated cancellation is idempotent: it returns the same Run, appends no
second event, and rewrites neither the original finish instant nor the original elapsed interval.

**A cancelled Run is its own terminal lifecycle.** `Run.status='cancelled'` is not `failed` with a
reassuring code. It carries no output, no usage, and **no provider error at all**, because refusing
to continue is not a provider outcome. A Run cancelled before execution began has no fabricated
`started_at` and a NULL `elapsed_ms`; a Run cancelled after it began preserves its real start and
reports the truthful interval until cancellation was accepted, using the same elapsed semantics C3
already used for a post-start recovery close. The internal Job does carry an infrastructure-owned
`execution_cancelled` code, because the frozen Job schema requires every terminal Job to explain
itself; that code is never produced by a provider and cannot steer retry policy, and the public Run
exposes none of it.

**The Worker is a follower, and that is the safety argument.** Every late write in the engine was
already fenced on a live claim — Job status, owning worker, claim token, and an unexpired lease — so a
stale Worker's success, failure, retry schedule, start, or heartbeat matches zero rows without any of
those paths knowing cancellation exists. Queued cancellation invokes no provider and invents no
Attempt; `retry_wait` cancellation keeps its due instant as append-only history while making it
unreachable; claimed-but-unstarted cancellation lands before the execution boundary; running
cancellation revokes authority while the Worker discovers it on its next heartbeat and stops waiting
on the local provider task. A cancelled Job cannot be claimed, retried, or requeued by C3's
reconciler, because a terminal Job is not a candidate for any of them.

**NervOS claims no remote cancellation.** Cancellation revokes *NervOS* authority, asks the local
provider task to stop, and guarantees that a late result can never be persisted. It does not claim
that the remote provider stopped processing a request it already received, that billing stopped, or
that remote side effects were rolled back. The API contract, the frontend copy, and ADR 0012 all say
so, and the provider adapters are unchanged.

**Execution timeout is not cancellation.** An Attempt's deadline comes from `provider_timeout_ms`,
already part of the Run's immutable limits snapshot, and starts only after the durable
execution-start commit, so queue time, claim time, a scheduled retry wait, and terminal persistence
are excluded, and each retry Attempt receives a fresh window. C5 owns two layers: the unchanged
cooperative inner deadline in `RunExecutor`, and a new provider-neutral outer watchdog in
`JobExecutionService` that exists because the inner bound cannot complete against a coroutine that
suppresses `CancelledError`. Both converge on `model_timed_out`, which remains `AMBIGUOUS`, is never
replayed, and never becomes a cancellation. The watchdog's winner rule is a fact about what already
happened: if the provider call has already completed when the deadline becomes ready, its real result
is consumed and a completed call is never relabelled as a timeout.

**Cleanup is bounded, and honestly so.** Python cannot forcibly terminate a coroutine that refuses to
die. C5 guarantees bounded local waiting, revoked durable authority, and no durable resurrection —
not that every coroutine disappears. A task that outlives the drain bound is left to finish on its
own, tracked until it completes so its eventual result or exception is retrieved and discarded rather
than reported as an unretrieved task exception; it holds no authority and every terminal write is
fenced, so it cannot overwrite or resurrect anything. `WorkerService` shutdown is bounded on the same
terms and still writes no cancellation state, because operational cleanup is not the owner's
decision — a stopping Worker leaves its claim for C3.

**Migration.** `0005_stage_c5_run_cancellation` rebuilds only the `runs` table to admit a fifth
lifecycle, reusing C3's proven rebuild discipline: an explicit column list, an autocommit section
around the foreign-key pragma, and a `PRAGMA foreign_key_check` that raises if integrity was not
preserved. It does not touch `jobs`, `job_attempts`, or `run_events`, creates no queue partition and
no `active_attempt_id`, and leaves `0001`–`0004` byte-identical. A database holding cancelled Runs
cannot be represented by C4's constraints, so the downgrade **refuses** rather than converting a
cancellation into a failure the user never had. C5 consumed migration number `0005`; C6 later added
`0006`.

**API and frontend.** `POST /api/v1/runs/{run_id}/cancel` returns the resulting Run: `200` for a
cancelled or already-cancelled Run, `409 run_not_cancellable` for a Run that already succeeded or
failed and is therefore never rewritten, and the same `404 run_not_found` for foreign and nonexistent
ids so cancellation cannot probe for another user's Runs. It requires the configured `Origin`, and it
exposes no Job, Attempt, token, worker, or disposition detail. The dashboard offers a Cancel control
on `created` and `running` Runs only; `cancelled` is terminal, so existing polling stops for it, and a
`retry_wait`-backed Run is publicly `running` and therefore cancellable without the frontend ever
learning an internal Job state.

**Verification.** C5 was verified with 18 focused timeout tests, 19 cancellation tests, 10
cancellation-race and timeout-ordering integration tests, and new C3/C4 regression guards. The
accepted candidate passed **617 Python tests**, **58 Worker tests**, **168 API tests**, **26
architecture guards**, **88 E2E-supervisor guards**, and **103 frontend tests**; Ruff lint and format;
Pyright with zero errors; frontend lint, typecheck, and production build; the repository security
scan (272 files, no findings); the migration upgrade/current/check/downgrade/re-upgrade lifecycle
through `0005`, including the downgrade refusal; the deterministic browser journey **twice**, in which
the cancellation journey proves a running Run becomes durably `cancelled` and that a Worker observes
the revocation; `check.py check`; and the isolated `clean-check` gate, whose pristine export passed
every step including the E2E. The protected migrations, ORM model module, provider adapters, retry
policy, and C3 reclamation module were byte-identical throughout, and the default `~/.nervos/nervos.db`
was unchanged.

## C7 implementation verification

C7 makes the execution kernel observable to its owner without giving the observability surface any
authority. It is recorded in ADR 0014. It is deliberately an observability milestone, not an
execution one: it changes no lease, no fence, no retry, no cancellation, no fairness, no cap, and no
provider invocation.

**Migration: none.** C7 consumes no migration number and adds no index, and the head remains
`0006_stage_c6_queue_partitions`. The decision rests on measurement rather than preference:
`UNIQUE(run_id, sequence)` already materialises in SQLite as an implicit index, and the pagination
predicate plans as `SEARCH run_events USING INDEX sqlite_autoindex_run_events_1 (run_id=? AND
sequence>?)` — a range seek on both columns with no temporary B-tree, no scan of other Runs, and the
`LIMIT` satisfied by index order. An explicit index would have been a named duplicate of one SQLite
already maintains. A second consequence is recorded because it shapes the tests: SQLAlchemy's
inspector does not report SQLite's implicit autoindexes, so C7 asserts the query **plan** rather than
the inspector's index list, and the migration suite's assertion about the `run_events` index set is
unchanged.

**One new public route.** `GET /api/v1/runs/{run_id}/events` is added to the existing Runs route
module, which is why `router.py` is not modified at all and **no architecture guard required
relaxation**. It is read-only by construction: the module carries no mutation verb for this
subresource, reaches no write primitive, and the application method it calls performs two `SELECT`s.
It cannot claim, start, retry, cancel, reconcile, heartbeat, execute provider work, or change Worker
state.

**Owner scoping reuses the Run's proven rule.** The service resolves `get_run(owner_user_id, run_id)`
first, which raises `RunNotFound` for a foreign Run and a missing one alike, so both produce the same
`404 run_not_found` through the one error envelope and event history can never be used to probe
whether another user's Run exists. Ownership is immutable after submission, so the two-read sequence
has no window in which a Run could change hands.

**The public projection is an allow-list**, asserted as an exact key set: `sequence`, `event_type`,
`created_at`, `attempt_number`, `code`, `message`, `available_at`. Four stored identifiers are
deliberately withheld — the global Event `id` (an AUTOINCREMENT value encoding system-wide volume,
where `sequence` is the Run-local order), `run_id` (implicit in the path), and `job_id`/`attempt_id`
(internal execution-obligation identity, where `attempt_number` already carries the label a reader
needs). No worker identity, claim token, lease, heartbeat, queue or fairness state, credential,
provider payload, prompt, or output is published, and none of those has a column in `run_events` in
the first place. `code` and `message` are *retained*: they are the sanitized error pair, bounded by
CHECK constraints, already public on the Run response, and exactly what makes a failed attempt
legible.

**The security boundary is the writer, not the reader.** `run_events` has no column for a token,
lease, worker identity, heartbeat, credential, provider payload, prompt, output, or environment
value; nowhere in the schema is there a JSON or free-form detail column; exactly one insert primitive
appends Events; and its free text is length-bounded by CHECK constraints rather than by convention.
Production writers normalize before persisting: a provider failure resolves to a frozen NervOS code
whose allow-list entry is a static sentence, and no production call site supplies free text. **There
is deliberately no response-time secret-redaction layer, and none is claimed** — the endpoint returns
the safe durable value it found and does not inspect, filter, or rewrite it. A database an operator
has manually poisoned with arbitrary text is outside this API's security guarantee. The supported
guarantee is narrower and stronger: text that reached the row through a production mutation path is
normalized, so the reader has nothing to strip.

**Ordering and pagination.** `sequence` is the authoritative order and `created_at` is display
metadata only: the sequence is allocated contiguously from a per-Run high-water mark read once inside
the writing transaction, so the stream is `1..N` with no gaps, and a batch is committed atomically.
Pagination is a keyset cursor — `sequence > after_sequence`, ascending, `limit` default 50 and
bounded to 1..200, out-of-range rejected with `422` — never an offset, which would shift under the
concurrent appends this stream is built from. The envelope carries `next_after_sequence`, so a client
loop terminates on `null` and a full final page costs one empty probe rather than a count query.
Because a writer allocates from `MAX(sequence) + k` in its own transaction, a reader sees a whole
batch or none of it, and a client that has applied up to sequence `S` can never have skipped an
Event. No snapshot token is required.

**Derived Run observability.** Run responses gain two additive read-only fields:
`execution_phase`, the Job's own durable status verbatim
(`queued | claimed | running | retry_wait | succeeded | failed | cancelled`, renaming nothing and
introducing no new Run status), and `retry_available_at`, present only while the phase is
`retry_wait`. Both are derived per read by joining the one Job behind the Run and are never
persisted, so neither can drift, and neither is authority: no module that mutates execution reads
them, cancellation eligibility remains gated on the Run's own status, and a phase that is stale the
instant it is serialised can hide no action from an owner entitled to take it.

**Timeline and polling.** The dashboard gains an expandable disclosure inside the existing Run card
that mounts its timeline only when opened, so a collapsed Run costs no request. All thirteen Event
types have a total compile-time copy mapping, so a new server-side type cannot render as a raw enum.
The hard ~300-second polling stop is **retired**: 2-second polling for the first 30 updates,
10-second polling afterwards, no hard stop while a displayed Run remains nonterminal, and a full
stop once every displayed Run is terminal. Event polling is incremental by sequence, merging pages by
a union keyed on `sequence` alone — never a timestamp, never array position — so duplicate delivery
renders one row and out-of-order responses cannot move the applied cursor backwards. When a Run
becomes terminal the timeline performs **one final catch-up drain cycle**, which may span several
requests, and then stops permanently. Copy keeps a timeout distinct from a cancellation, never
asserts that a Worker died, and preserves the limit that cancellation claims no remote stop.

**No Attempt API, no Worker API or dashboard, no SSE/WebSocket, and no queue-position or fairness
surface.** An Attempt API was declined on safety as well as scope: `job_attempts` does hold
`claim_token`, `worker_id`, `lease_expires_at`, and `last_heartbeat_at`, so an endpoint over it would
carry a permanent obligation to project those columns away correctly on every response, whereas the
Event timeline represents the whole user-facing attempt history from a table with nothing to redact.

## C8 implementation verification

C8 is integrated deterministic acceptance for Stage C, not a feature milestone. It adds **no**
product architecture: no API, no status, no table, no migration, no execution policy, no queue
policy, no transport, no configuration, and no authority mechanism. No production defect was found
after the C7 source freeze, so no source re-freeze and no evidence rerun was required.

**Integrated matrix A–L.** Every scenario composes the real modules C2–C7 shipped rather than
re-implementing a transition, and every scenario ends by checking that the public Event timeline
agrees with the durable final state: normal success; pre-start Worker loss recovered and executed
exactly once; post-start loss closed as ambiguous with no replay at any later instant; a positively
safe rate limit retried while still honouring all three caps; a committed retry surviving a restart
and resuming when due; pre-start cancellation making zero provider calls; running cancellation
fencing every late write; an execution timeout failing as ambiguous with no retry and an event set
disjoint from cancellation; the three concurrency dimensions observed never exceeded; fairness
rotating while recovery and retries interleave; three-dimensional backpressure rejecting atomically
and releasing without leak; and the timeline agreeing in every terminal shape.

**Restart acceptance.** Durable state survives engine teardown, runtime recomposition and reopen: a
queued Job, a `retry_wait` Job with its exact due instant, the `queue_partitions` fairness history,
Worker incarnation and staleness, and every Run and Event. The suite disposes and reopens the
database engine and rebuilds the composition inside one test process; it does **not** spawn a second
interpreter, and it does not claim to. What it establishes is the property that matters: no
correctness depends on runtime in-memory state, only on the database file.

**Multi-Worker composition and bounded stress.** Two and three Workers over one database with
separate engines assert that unrelated capacity is unaffected when one loses authority, and that
recovery, retries, cancellation and Event ordering still compose. The bounded soak covers the frozen
scale of 4 Agent Instances × 2 providers × 3 heterogeneous Workers × 120 Jobs with outcomes assigned
deterministically by index — never at random — and time advanced by parameter rather than by waiting
on real backoff. Across it: no global, per-Agent or per-provider cap was ever exceeded; no Job ever
held two live Attempts; every continuously eligible Agent partition was served; no pending-capacity
leak remained after the workload drained; every Run settled truthfully; and no Event sequence
collided or gapped.

**Integrity, migration lifecycle and security.** `PRAGMA integrity_check` returns `ok` and
`PRAGMA foreign_key_check` is empty on the acceptance database, with active-Attempt uniqueness,
`jobs.attempt_count` agreement with the durable Attempt count, and no dangling fairness marker
asserted alongside. The migration lifecycle is exercised fresh-to-head, stepwise, and through the
supported downgrades, with the head confirmed still `0006` and no `0007` or `0008`. A composed
security acceptance drives a raw provider exception carrying a key, a private-key marker and an
authorization value through the production execution path and asserts the secret reaches no durable
text and no public surface, and that every durable message is exactly the allow-list entry for its own
durable code.

## C6 implementation verification

C6 completes the queue-control layer: it makes execution concurrency authoritative, replaces global
FIFO selection with durable Agent fairness, and adds per-dimension admission backpressure. It is
recorded in ADR 0013.

**Execution concurrency is now authoritative.** The active predicate is a Job with
`status IN ('claimed','running')` **and** `lease_expires_at > now`, so a Job that is claimed but has
not started still holds its slot while `queued` and `retry_wait` hold none. A Job is claimable only
if the **global**, its **Agent Instance's**, and its **provider's** limits all have room — an
intersection decided at claim time from live Jobs inside the one short `BEGIN IMMEDIATE` claim
transaction. Nothing is persisted, so no counter can drift after a crash, a recovery, a retry, or a
cancellation; an expired lease simply stops consuming capacity, and that is safe because an expired
Job is still `claimed`/`running` and therefore unreachable from the claim's source predicate — C3
reclamation remains the only path that returns it to the queue.

The three limits are one code-level policy in `nervos-core`, defaulting to **4 global / 4 per Agent
Instance / 4 per provider**. They are deliberately not environment settings: each claim compares a
database-wide count against its limit, so two Workers holding different values would let the fleet
run above the intended bound. The defaults equal the global limit, so they preserve existing
out-of-the-box behavior; the per-Agent and per-provider mechanisms are authoritative and are proven
by tests that inject lower policy values. `NERVOS_MAX_ACTIVE_JOBS` is retained but is now
**tightening-only**: it may lower what a Worker claims and can never raise the authoritative limit.
Runtime-tunable active policy is explicitly deferred.

**Selection is durable least-recently-served per Agent Instance.** The C6 plan proposed deriving a
round-robin cursor from the most recent *global* Attempt; external review rejected it on evidence.
That design assumed a common eligible set across Workers, and Workers do not share one — a Worker
that can run only `anthropic` cannot see an `openai`-only partition. The review's counterexample
(Worker X eligible to `{55}`, Worker Y eligible to `{50,60}`, where Y always computes "the first
partition after 55" and therefore never serves 50) showed the proof was wrong, not merely incomplete.

The implemented design stores one durable fact per Agent Instance in `queue_partitions`:
`last_served_attempt_id`, the `job_attempts.id` of the most recent committed claim for that
partition, NULL when it has never been served. Selection filters the claimable set by everything
that makes work runnable now — legal Job/Run shape, due `available_at`, remaining Attempt budget,
this Worker's provider capability, and all three capacity limits — then chooses the **never-served
partition first**, then the **smallest marker**, with `agent_instance_id` as a deterministic
tie-break, and finally the **oldest due Job** inside it (`available_at ASC, id ASC`). The marker is
written by the same transaction that inserts the Attempt, so fairness advances **only when a claim
commits**; a poll that finds nothing, loses a filter, hits a cap, loses the compare-and-set, or
rolls back advances nothing and penalizes no partition, and a skipped partition re-enters at its own
position. Global FIFO is intentionally no longer the scheduling contract.

**Blocked work no longer blocks the queue head.** A partition whose Agent is at its limit, whose
provider is at its limit, or whose provider this Worker cannot run is filtered out *before*
selection, so it cannot head-of-line block work the Worker can actually execute. This is the
substantive behavioral change from global FIFO. The guarantee is bounded and honest: for a given
compatible claiming capability set, continuously eligible Agent partitions cannot be repeatedly
bypassed by more-recently-served competitors — no cross-capability global guarantee is claimed,
because fairness is not promised for work no present Worker can run.

**Admission backpressure gains two dimensions.** `NERVOS_MAX_PENDING_JOBS_PER_AGENT` and
`NERVOS_MAX_PENDING_JOBS_PER_PROVIDER` join the existing global cap. All three are counted, and the
Run and Job written, inside the same admission transaction, so a rejection leaves no partially
created Run and a concurrent last-slot race produces exactly one winner. Each defaults to the global
bound, so neither binds until an operator lowers it; lowering one reserves queue headroom so a
single Agent Instance or provider backlog cannot refuse every other submitter. Public rejection is
unchanged and generic — `429 queue_capacity_exceeded` — and discloses no dimension, queue position,
partition state, or count.

**Migration.** `0006_stage_c6_queue_partitions` adds exactly one table,
`queue_partitions(agent_instance_id, last_served_attempt_id)`, holding durable fairness history and
nothing else: no status, no lease, no claim token, and no active or pending counter. `Jobs` remain
the only durable execution obligation, and the Job lease plus Attempt token remain the only
execution authority. The marker deliberately carries no foreign key — it is a monotone sequence
that is only compared, never dereferenced. The migration deterministically backfills one row per
Agent Instance that already has Jobs, taking `MAX(job_attempts.id)` as the marker or `NULL` for an
Instance that has never been claimed, so pre-existing data behaves as never served and no execution
row is rewritten. `0001`–`0005` are unchanged. Unlike 0005, the downgrade **drops the table without
refusing**, because it is derived scheduling metadata that rewrites no execution history; a later
re-upgrade reconstructs it from the same backfill. The Worker's expected schema revision is now
`0006_stage_c6_queue_partitions`; C6 promises no mixed C5/C6 Worker rolling operation during that
transition.

**API and frontend.** C6 adds no public structural API and no frontend change. The admission error
stays `429 queue_capacity_exceeded` with its existing safe message, and no partition, cursor, active
count, or saturation state is exposed.

**Verification.** C6 was implemented main-agent-only, then verified with focused suites: 16 fairness
tests (including the heterogeneous-capability counterexample as a regression test), 17 policy tests,
13 concurrency-cap tests, 12 admission-backpressure tests, 5 query-plan tests, 8 multi-Worker tests,
and 5 new migration tests for `0006`. The accepted candidate passed **758 Python tests** across
`packages` and `apps` (367 core, 66 Worker, 183 API, 27 architecture, 142 provider-adapter), the
migration upgrade/current/check/downgrade/re-upgrade lifecycle through `0006` (19 passed), the
deterministic multi-Agent acceptance proof in which a newcomer Agent is served ahead of an older
backlog, and the existing deterministic browser journey (exit 0). Ruff lint and format are clean,
Pyright reports zero errors, the frontend suite (103 tests) plus lint, typecheck, and production
build pass, the repository security scan is clean, `check.py check` passes with 846 tests, and the
isolated `clean-check` gate passes. The protected migrations, ORM domain modules, provider adapters,
retry policy, and C3 reclamation module were byte-identical throughout, and the default
`~/.nervos/nervos.db` was unchanged.

**Explicitly not provided by C6.** No runtime-tunable active policy, no per-Agent-Instance custom
limits, no scheduler daemon, no leader election, no queue dispatcher, no preemption of running Jobs,
no public queue-position API, and no public Run Events, Attempt, or Worker surface — those remain
C7. C6 adds no new Run, Job, or Attempt status and no new Run Event type.

## C2 implementation verification

C2 changes the product from awaited API-process execution to durable asynchronous execution. `POST /api/v1/agent-instances/{id}/runs` now returns `202 Accepted` after committing exactly one immutable `Run(status=created)`, one `Job(status=queued)`, and the initial `run.created`/`run.queued` events. The API/control plane validates ownership, exact `nervos.chat@1`, input bounds, and known provider identifiers, but it holds no provider credential, constructs no provider SDK client, composes no handler registry, and cannot claim/start/heartbeat/terminalize Jobs.

A separate `apps/worker` uv workspace member runs the execution plane. The Worker validates the existing Alembic revision, resolves its configured providers once at startup, claims only Jobs for providers it can execute, creates one active Attempt under `BEGIN IMMEDIATE`, commits the execution-start boundary before any provider call, renews the lease while execution and finalization run, invokes the existing trusted Chat path exactly once per Attempt, and atomically terminalizes Attempt + Job + Run with safe Run Events. Claim tokens, prompts, outputs, provider bodies, raw exceptions, and credentials are never logged or written to Run Events.

The dashboard renders a `created` Run as **Queued** and distinguishes the queued and running pending states with truthful copy, then observes the terminal result through polling that runs only while a Run is nonterminal and is bounded, so a stranded Run is never presented as actively progressing. Reloading reads the persisted result rather than browser state.

C2 enforces a hard global pending cap (`NERVOS_MAX_PENDING_JOBS`) in the submission transaction and a node-wide active cap (`NERVOS_MAX_ACTIVE_JOBS`) in the claim transaction, with configurable per-process Worker concurrency (`NERVOS_WORKER_CONCURRENCY`). (C6 later made execution concurrency authoritative with global, per-Agent-Instance, and per-provider limits, and redefined `NERVOS_MAX_ACTIVE_JOBS` as a tightening-only ceiling that can never raise the authoritative limit; see the C6 section for the current behavior.) Claiming is capability-aware and one-winner: a Worker claims only Jobs whose provider it is configured for, and two Workers racing one Job produce exactly one claimant. Each claim rotates a 32-byte claim token and persists one Attempt; the execution-start boundary commits before any provider call; and an independent heartbeat renews the lease across every lease window, including finalization. Every owner write is fenced on ownership **and** an unexpired lease, so lease expiry is authority loss — an expired Worker discards its result, never overwrites, and never requeues. A Worker with no provider credentials starts successfully and claims nothing. A known-provider Run with no capable Worker remains queued rather than being failed. Failed Attempts record `SAFE_TO_RETRY`, `DO_NOT_RETRY`, or `AMBIGUOUS` as evidence, but C2 never writes `retry_wait` and never retries execution. Persistence-finalization retry replays only the fenced terminal database transaction and never re-invokes a model.

Still absent after C2: expired-lease recovery and reconciliation, automatic execution retries, retry scheduling, cancellation, per-Agent concurrency, per-provider concurrency, fairness, queue partitions, a public Run Events API, an event-timeline UI, SSE and WebSockets, a Workers table/registry, worker-health tracking, scheduling, tools/MCP, memory, package installation, marketplace, and persistent secret management.

At C2 completion the migration head was `0003_stage_c1_durable_execution`; no `0004` existed. C2 changed no migration, no ORM model module, and no provider adapter. (C3 later added `0004_stage_c3_worker_registry`; see the C3 section.)

At C2 completion, lease renewal was liveness, not crash recovery. A Job that was already `claimed` or `running` when its Worker died remained stranded — neither retried nor requeued — because C2 shipped no reconciler. C3 supersedes this: expired claims are now reconciled automatically. Legacy `running` Runs with no Job can be closed only by the explicit operator command `python -m nervos_worker --reconcile-legacy-runs`; legacy `created` Runs with no Job are left untouched.

C2 verification added durable submission, capacity, claim-concurrency, terminalization, lease/heartbeat, contention/replay, Worker-loop, Worker-config/credential, multi-Agent, API, frontend polling, E2E-supervisor, and architecture-guard coverage. The accepted candidate passed 642 Python tests, 46 Worker tests, and 91 frontend tests; Ruff lint and format; Pyright with zero errors and zero warnings; frontend lint, typecheck, and production build; the repository security scan (258 files, no findings); the migration upgrade/current/check/downgrade/re-upgrade lifecycle on a disposable database; the deterministic API + Worker + Web browser journey twice; and the isolated `clean-check` gate. The protected migrations, ORM model module, and both provider adapters were byte-identical throughout, and the default `~/.nervos/nervos.db` was unchanged. Every check passed on GitHub-hosted runners for pull request [#11](https://github.com/Rishiraj-Yadav/NervOS/pull/11).

## C4 implementation verification

C4 activates durable execution retry for the one failure class that is positively safe to replay. It adds **no migration**: the C1 schema already reserved `retry_wait`, `available_at`, `retry.scheduled`, and per-Attempt retry dispositions, and C4 writes them from the application and persistence layers. Migration head remains `0004_stage_c3_worker_registry`, and `0001`–`0004` are byte-identical to their accepted C3 state.

**Only one outcome may be replayed.** `model_rate_limited` is the sole normalized failure carrying `SAFE_TO_RETRY`, because it is evidence that the provider declined the request. A timeout, an unavailable transport, an internal failure, and any unrecognized code stay `AMBIGUOUS` and terminal, so an outcome whose remote result cannot be excluded is never blindly replayed. The scheduling transaction re-derives that restriction from the exact code as well as the disposition, so widening the classification map alone cannot widen the replay surface. Provider SDK retries remain `max_retries=0`, adapter code is unchanged, and there is no provider or model fallback.

**Retry is durable state, not a timer.** A safe failure closes the current Attempt as immutable failed evidence (`failed`, `SAFE_TO_RETRY`, the original normalized error, its real start and finish), moves the Job to `retry_wait`, stores the absolute due instant in `available_at`, clears every claim field, and appends `attempt.failed` then `retry.scheduled` — with **no** intermediate `run.failed`. C4 adds **no scheduler daemon, timer row, or held execution slot**: a retry becomes eligible when ordinary Worker polling sees `retry_wait` with `available_at <= now`, and a due retry is claimed through the same `BEGIN IMMEDIATE` path as queued work, with capability filtering, the global active cap, and one-winner semantics intact. `available_at` is an earliest-eligibility boundary, never an appointment. `retry_wait` holds no lease, so it consumes no active capacity; it remains one pending obligation.

**The Run survives the retry.** A Run that has begun executing is still executing while it waits, so it stays `running` with no output, usage, elapsed time, or error, and its original `started_at` is never rewritten. C4 adds no public retry status: read-only Run projections show a still-running Run, and an internal retry is asserted not to leak Job status, attempt number, disposition, deadline, or claim data. Each retry creates a fresh numbered Attempt with a fresh 32-byte token and lease, fenced exactly like a first claim, so a previous incarnation's authority is never revived.

**Backoff is deterministic.** The schedule is 1s, 2s, then 4s capped, with no jitter, no `Retry-After` parsing, and no provider-specific policy. The scheduling anchor is captured **once** per logical finalization and stays fixed across every safe database-only replay, so a replayed transition recomputes the identical deadline and an uncertain COMMIT has exactly one expected value to reconcile against. The transaction's own clock is used only for lease and fencing checks.

**The backoff ordinal is not the claim counter.** It counts this Job's *started* safe execution failures. A Worker that died before the execution-start boundary consumed claim budget but never called a provider, so it does not slow a retry for a request that never happened. `max_attempts` remains a **shared total committed-claim budget**: a pre-start Worker loss consumes a slot a provider retry might otherwise have used. C4 accepts that coupling rather than adding a second hidden counter, because the alternative would either delete execution history or remove the only existing bound on a Worker that crashes deterministically before every start. When the budget is spent, the Job and Run terminalize with the **original** `model_rate_limited` and no `retry.scheduled`; no `retry_exhausted` code was introduced.

**Safety is unchanged where it matters.** The provider is invoked at most once per durably started Attempt, and a database persistence failure never replays it: only proven-uncommitted SQLite BUSY/LOCKED failures are replayed, on a fresh connection, and an uncertain COMMIT is resolved by reading durable state back and adopting the committed result rather than repeating the transition. A stale or expired claim cannot schedule anything — its write affects zero rows and appends no event. A crash **before** a `retry_wait` commit leaves a started Attempt that C3 closes as `execution_outcome_ambiguous` and never replays: NervOS declines a legitimate retry rather than replay a request whose outcome it cannot exclude. A crash **after** the commit leaves a claimless durable retry that needs no recovery and survives a restart, verified by a new Worker incarnation claiming it. C3's own reconciliation still selects only expired `claimed`/`running` claims; expired pre-start `SAFE_TO_RETRY` evidence is never read as an instruction to retry.

**Accepted limitations, recorded rather than hidden.** Run `elapsed_ms` and Run usage remain the **terminal** Attempt's values; they do not include earlier Attempts, retry-wait time, or total Run wall-clock duration, and C4 provides no cumulative cross-Attempt token accounting (per-Attempt accounting would need a schema revision, which C4 does not add). The bounded read-only UI poll (roughly 300 seconds at 2-second intervals) is unchanged and is **not** a completion guarantee: a Run's durable state stays correct across Worker downtime, provider duration, pre-start loss, lease recovery, or host downtime, and a later reload or refetch shows the current state. Richer execution observability remains C7.

> **Superseded by C7.** The ~300-second polling bound above was retired, not raised. It existed so that an abandoned Run could not be presented as actively progressing; with a durable execution timeline and a truthful derived execution phase, a Run that is not progressing now renders as exactly that, so the UI no longer needs a request budget to stay honest. Current behaviour is 2-second polling for the first 30 updates, 10-second polling afterwards, no hard stop while a displayed Run remains nonterminal, and a stop once every displayed Run is terminal. The cumulative cross-Attempt token-accounting limitation above still stands.

**Explicitly not provided by C4.** No cancellation (C5), no fairness, queue partitions, or per-Agent/per-provider concurrency (C6), and no public Run Events API, timeline, Worker dashboard, SSE, or WebSockets (C7). C4 does not read, write, or prioritize `cancel_requested_at`, and C4 ships no cancellation endpoint, no `cancelled` Run status, and no cancel-versus-retry rule. C4 does not claim exactly-once execution and claims no provider-side rollback or cancellation. (C5 later implemented owner cancellation and execution-timeout orchestration, C6 later implemented authoritative concurrency, durable Agent fairness, and admission backpressure, and C7 later implemented the read-only Run Events API, the execution timeline, and the polling model. A Worker dashboard, an Attempt API, and SSE/WebSockets remain unimplemented. The C4 sections remain the historical record of the state C4 shipped.)

**Verification.** The accepted C4 candidate passed 71 focused C4 tests; the full Python suite of 704 tests (55 Worker, 158 API); 26 architecture guards; 92 frontend tests; 21 E2E-supervisor tests; Ruff lint and format; Pyright with zero errors and zero warnings; frontend lint, typecheck, and production build; the tracked-file security scan (265 files, no findings); and an `EXPLAIN QUERY PLAN` check proving the widened due-work query still resolves through the existing `jobs` status index rather than scanning. Migration head was confirmed still `0004` with exactly four migrations. A deterministic supervised browser journey proves the retry end to end — a scripted rate limit, a durable `retry_wait`, a still-running Run, a second Attempt after the due instant, and exactly two provider calls for that Run — and asserts from committed state that the retry's own `claimed_at` is not before its `available_at`, that the Run's `started_at` equals the first Attempt's start, and that no `run.failed` occurred. It passed twice consecutively, and `check` and the isolated `clean-check` gate both passed. Protected files were byte-identical throughout and the default `~/.nervos/nervos.db` was unchanged (size 65536, sha256 `f60ed2b32637d314d31a4305c704b5f80ff0db14adbefdff156368cc5f05800f`). No live provider request was made.

## C3 implementation verification

C3 activates durable Worker liveness and recovery. It passed external source review of its plan, external implementation review, and a dedicated E2E acceptance remediation.

**Migration.** `0004_stage_c3_worker_registry` creates the `workers` table and performs one narrow, externally approved `runs` lifecycle evolution. `0001`, `0002`, and `0003` are byte-identical to their C2 state. SQLite cannot alter a CHECK in place, so `runs` is rebuilt value-for-value inside Alembic's `autocommit_block()` — necessary because the migration harness installs `PRAGMA foreign_keys=ON` and wraps migrations in a transaction, which makes an in-transaction foreign-key disable a silent no-op and would fail the drop against `runs`' two RESTRICT children. A `PRAGMA foreign_key_check` afterwards raises if integrity was not preserved. The downgrade refuses rather than rewriting history if any Run was closed before execution started.

**Worker registry.** Each process incarnation registers a durable row and never recycles another incarnation's identity; a restart draws a new id and therefore a new row. `worker_id` is unique, and the row carries only identity and liveness timestamps. The registry heartbeat is **monotonic**: the write refuses to move `last_heartbeat_at` backwards, so a wall-clock adjustment cannot make a healthy process look older. Health is **derived** from timestamps — `stopped` when `stopped_at` is set, otherwise `healthy` or `stale` by heartbeat recency — and is never stored as a mutable state, so it cannot contradict its own evidence. **Stale means "not observed recently", never "definitely dead".**

**Authority.** Worker registry health is observability. The **Job lease is execution authority.** A successful registry heartbeat that finds no live row for this incarnation (or a stopped one) contradicts the invariant that every executing Worker is registered, so the Worker stops accepting new claims and shuts down in an orderly way; in-flight work keeps its own lease. Staleness of a registry row never reclaims a Job.

**Recovery.** Reconciliation selects a Job with `status IN ('claimed','running')` and `lease_expires_at <= now`, re-reads its active Attempt, and CAS-checks the exact claim tuple before mutating. Classification uses only the committed execution-start boundary:

- *Pre-start* (`execution_started_at IS NULL`): the boundary provably never committed. With claim budget remaining, the Attempt becomes `expired` with `SAFE_TO_RETRY` and the Job returns to `queued` with a fixed backoff, so a deterministic crasher cannot be re-claimed in a hot loop; the Run stays `created`. When the claim budget is exhausted, the Job and Run are closed truthfully (below).
- *Post-start* (`execution_started_at IS NOT NULL`): a provider call may have been issued. The Attempt becomes `expired` with `AMBIGUOUS`, and the Job and Run fail with `execution_outcome_ambiguous` using the Run's **real** `started_at` and a **real** `elapsed_ms`. It is **never replayed**.

No provider call is made on any recovery path. Reconciliation runs once at Worker startup, bounded, and periodically thereafter, and needs no configured provider — a credential-free Worker still reconciles. Concurrent reconcilers are one-winner via `BEGIN IMMEDIATE` plus row-count CAS with no leader election.

**The narrow failed-before-start Run shape.** When repeated pre-start Worker loss exhausts a Job's claim budget, the Run is closed as `status='failed'` with `started_at` NULL, `elapsed_ms` NULL, no output, no finish reason, no usage, and `error_code='worker_recovery_exhausted'`. The Run-lifecycle CHECK was relaxed for exactly this case and no other: the started-`failed` branch excludes that code, and the never-started branch requires it. Execution provably never began and no model request was issued for those Attempts, so NervOS records no `started_at` and no `elapsed_ms` rather than fabricating them. **This is not cancellation** — it is an infrastructure recovery-budget exhaustion, and C5 still owns cancellation.

**Explicitly not provided by C3.** No automatic model/provider execution retries, no `retry_wait` scheduling, no execution backoff policy, no cancellation, no per-Agent or per-provider concurrency, no fairness or queue partitions, no public Run Events API, no event timeline, no Worker dashboard, no SSE/WebSockets. Provider SDK retries remain `max_retries=0` and there is no provider or model fallback. C3 does not claim exactly-once execution. C4 later activated the execution retry C3 deliberately left out, and C5 and C6 later added cancellation and queue control; the C3 sections below remain the historical record of the state C3 shipped.

**Verification.** The accepted C3 candidate passed 661 Python tests (54 of them Worker tests) and 92 frontend tests; Ruff lint and format; Pyright with zero errors and zero warnings; frontend lint, typecheck, and production build; the repository security scan (261 files, no findings); the architecture guards (24, including new C3 boundary guards); the migration upgrade/current/check/downgrade/re-upgrade lifecycle — including a populated `0003` database — on disposable paths; a dedicated deterministic pre-start recovery browser journey that asserts the durable timeline `attempt.claimed → attempt.expired → recovery.pre_start → attempt.claimed → attempt.started → run.succeeded`, that the abandoned Attempt never crossed the execution-start boundary, and that zero provider calls occurred before recovery; the full deterministic API + Worker + Web journey repeatedly; `make check`; and the isolated `clean-check` gate. Protected files were byte-identical throughout, the default `~/.nervos/nervos.db` was unchanged, and no live provider request was made.

## C0 architecture verification


C0 was an architecture freeze and governance milestone only: it changed no repository file, added no schema, and implemented no behavior. It fixed the durable single-host execution engine boundaries that C1 onward must honor — the separation of Agent Definition, Agent Instance, Run, Job, Attempt, Run Event, and Worker; the Run, Job, and Attempt state vocabularies; the internal `SAFE_TO_RETRY`/`DO_NOT_RETRY`/`AMBIGUOUS` retry disposition; the rule that worker loss before external execution starts is safely recoverable while loss after `execution_started_at` is ambiguous and never blindly replayed; `BEGIN IMMEDIATE` claim serialization with commit before dispatch; the partial unique active-Attempt index; and per-Run `MAX(sequence) + 1` Run Event allocation.

## C1 implementation verification

C1 adds the durable execution foundation without changing product behavior. Migration `0003_stage_c1_durable_execution` creates `jobs`, `job_attempts`, and `run_events`; migration head is now `0003_stage_c1_durable_execution`, and `0001` and `0002` are unchanged.

The durable foundation is dormant. A Job is one internal durable obligation per Run, one Attempt is one claim/execution episode for a Job, and a Run Event is an append-only safe lifecycle fact sequenced within one Run and carrying only narrow typed safe fields. The single partial unique index `uq_job_attempts_one_active` permits at most one active `claimed` or `running` Attempt per Job while leaving multiple historical terminal Attempts legal. The `SAFE_TO_RETRY`, `DO_NOT_RETRY`, and `AMBIGUOUS` retry dispositions are persisted, but no retry engine consumed them at C1 — C4 later activated only the `SAFE_TO_RETRY` path, and `DO_NOT_RETRY` and `AMBIGUOUS` still never retry. `jobs.cancel_requested_at` is the sole dormant future cancellation-request authority, and no cancellation behavior exists. (C5 later activated it; this section remains the historical record of the state C1 shipped.)

At C1 completion, before the C2 cutover, the atomic Run + Job + initial-event submission primitive and the per-Run event appender had no production caller. The public `POST /api/v1/agent-instances/{id}/runs` route was unchanged and still executed synchronously, returning HTTP 201 with the terminal Run, so normal Stage B execution created no Job, Attempt, or Run Event row, and no Worker process existed. C2 replaced that route behaviour with durable HTTP 202 acceptance and a separate Worker; see the C2 section above for the current state. No `active_attempt_id` exists in either the schema or the domain.

Schema-parity protection: `alembic check` does not compare SQLite CHECK constraints, so a permanent integration test asserts the migrated schema against the ORM metadata on constraint names, normalized expressions, server defaults, foreign keys, and indexes, and fails if that contract drifts. A pre-acceptance audit found the applied schema was weaker than the ORM declared — 19 CHECK constraints were missing, four expressions differed, and `jobs.max_attempts` lacked its `DEFAULT 3` — and migration `0003` was corrected in place, so the applied schema and the ORM metadata now agree exactly for every table. A negative control confirms the guard fails when a constraint is removed.

Verification: the full Python suite (569 tests), the frontend suite (85 tests), Ruff lint and format, Pyright, the security scan, the migration upgrade/downgrade/re-upgrade lifecycle, the deterministic offline E2E journey, `check`, and `clean-check` all passed. No live provider request was made, and the default database `~/.nervos/nervos.db` was unchanged. C1 passed external implementation and remediation review, was finalized as implementation commit `8e9c9da`, and was merged to `main`.

## Accepted Stage C limitations

These are recorded rather than hidden, and they are not softened elsewhere in the documentation.

**Exactly-once is not guaranteed.** NervOS does **not** guarantee exactly-once remote provider
execution. What it does guarantee is narrower and verifiable: **fenced durable authority**, so only a
live lease holder may write; **no blind replay of ambiguous execution**, so an unknown outcome is
closed as failed rather than repeated; **safe retry only for a positively safe outcome**, checked
twice; and **late stale writes cannot overwrite truth**. Remote provider processing, billing, and side
effects may still occur after an ambiguous post-start crash, a running cancellation, or an execution
timeout — they remain outside the local transaction boundary, and no local mechanism can undo them.

**No provider fallback.** A provider failure fails the Run; it is never re-routed to another
provider or another model. SDK-level retries remain disabled in both adapters (`max_retries=0`).

**Remote cancellation is not guaranteed.** Cancelling stops *NervOS* from waiting for and persisting
a result. It does not and cannot prove that a provider stopped processing a request it already
received, so cancellation claims no remote stop, no billing stop, and no remote rollback.

**A non-cooperative coroutine cannot be forcibly killed.** The outer watchdog observes and records;
it does not terminate Python work that refuses to yield.

**Concurrency policy is not runtime-tunable**, and there is no per-Agent-Instance custom limit. The
policy constants are shared and deliberately rigid — a claim compares a database-wide count against
its limit, so a Worker holding a different limit would let the fleet run above the intended bound
(ADR 0013).

**Observability is polling, not streaming.** There is no SSE, no WebSocket, and no long-poll
transport. The Event timeline is sequence-incremental and lossless, and a page left open polls slowly
for as long as a displayed Run is nonterminal.

**No public scheduling topology.** Queue position, partition, fairness rank, active and pending
counts, and Worker identity are not public anywhere.

**No Worker API or dashboard, and no separate Attempt API.** Worker registry `stale` means "not
observed recently", never "dead", and no Worker surface is public in Stage C. Attempt history is
reached through Run Events rather than a dedicated endpoint.

**Token accounting is terminal-Attempt-centric.** Run `elapsed_ms` and Run usage remain the terminal
Attempt's values; they exclude earlier Attempts, retry-wait time, and total Run wall-clock duration,
and cumulative cross-Attempt token accounting is not implemented.

**`execution_phase` is convenience, not authority.** It is derived per read, never persisted, and
stale by nature; nothing that mutates execution reads it.

**And the durable execution database is SQLite, single-node.** Stage C is one SQLite database on one
host; there is no distributed coordination, and no claim about behaviour under a multi-writer
deployment is made.

## Next action

C7 and C8 are complete and externally accepted, and Stage C — the persistent execution engine — is **complete**.

E0 through E5 are complete and externally accepted. Stage E is closed: integrated acceptance,
security, recovery, and documentation verification preserve the one ordinary Run/Job execution seam.

C7 makes the kernel legible without giving it any new authority. An owner can read their Run's durable execution timeline through one owner-scoped, read-only endpoint, ordered by the Run-local `sequence` the writers allocated, paginated by a keyset cursor rather than an offset so a concurrent append can never be skipped. Run responses additionally carry a derived execution phase — the Job's own durable status, verbatim, with a retry instant only while one is pending — which is what finally distinguishes "executing now" from "waiting to retry". Both are computed per read and never persisted, and no module that mutates execution reads them: observability is a projection, never a control. The dashboard gains an expandable timeline, incremental polling, and a polling lifetime that no longer abandons a live Run after roughly 300 seconds. C7 added **no migration**: the existing `UNIQUE(run_id, sequence)` index already serves the pagination predicate as a bounded range seek, so the migration head remains `0006_stage_c6_queue_partitions`.

C8 proves that the finished kernel composes. Each of C2–C7 proved its own slice in isolation; nothing had proved the slices hold together, so C8 adds an integrated acceptance matrix spanning normal execution, pre-start recovery, post-start ambiguity, safe retry, retry across restart, pre-start and running cancellation, execution timeout, all three concurrency dimensions, durable fairness, and backpressure — and checks in every case that the public timeline agrees with the durable final state. It adds restart acceptance (durable state survives engine teardown, recomposition and reopen), multi-Worker composition, a bounded deterministic 120-Job stress soak over 4 Agent Instances, 2 providers and 3 heterogeneous Workers, SQLite integrity checks, the migration lifecycle, and security/leak acceptance. **C8 changed no product architecture**: it added acceptance evidence and nothing else.

Taken together, the guarantees NervOS now offers are: **fenced durable authority**, so only a live lease holder may write; **no blind replay of ambiguous execution**, so an unknown outcome is closed as failed rather than repeated; **safe retry only for a positively safe outcome**; and **late stale writes cannot overwrite truth**. NervOS does **not** guarantee exactly-once remote provider execution. Remote provider processing, billing, and side effects may still occur after an ambiguous post-start crash, a running cancellation, or an execution timeout, and these remain outside the local transaction boundary.

D7 is complete and externally accepted, and Stage D — the tool and MCP layer — is **complete**. **E0 is complete and externally accepted**: Stage E — scheduling, events, and triggers — has its architecture frozen in **ADRs 0018–0020**, and it changed no runtime behaviour. **E1 is complete and externally accepted**: the durable trigger/occurrence domain and migration `0008` exist, and the Run-creation seam is shared and atomic. **E2 is complete and externally accepted**: a dedicated Scheduler process evaluates one-time, interval and cron schedules with IANA timezone and frozen DST semantics, coalesces missed occurrences under `COALESCE_ONE`, and materializes each due schedule into an ordinary Run atomically, with duplicate/stale protection across concurrent schedulers and deferral under admission backpressure. The next engineering milestone is **E3 — webhook ingress**: secret authentication and rotation, and the idempotency rules that make a repeated delivery resolve to the occurrence it already produced.

**E2 is implemented and complete**, and it is the milestone that makes a schedule trigger *fire*. A dedicated `apps/scheduler` process — holding no provider credential and opening no remote connection — polls on a fixed five-second interval, reads a bounded page of at most 32 due schedules in a stable `(next_fire_at, id)` order, and hands each decision to one `BEGIN IMMEDIATE` transaction that verifies it against durable state before applying it. All three schedule kinds are evaluated: one-time fires once and completes; interval advances on an exact fixed-duration sequence with O(1) misfire arithmetic, so a three-day outage produces exactly one catch-up occurrence rather than 864; and cron iterates through a single `cronsim` adapter confined to one infrastructure module, on the zone's local wall clock, with every local-to-UTC conversion performed by NervOS in the domain. The DST contract is therefore met by NervOS rather than by a library: a nonexistent local time fires once at the gap-end transition instant — `30 2 * * *` in `America/New_York` on 2026-03-08 resolves to `07:00Z`, which is neither value `zoneinfo`'s `fold` offers, so the transition is located by a bounded bisection over a window exactly one gap wide — and a repeated local time fires once, on the first occurrence, suppressed structurally because iteration happens in wall-clock space. Non-hour shifts are handled, `Australia/Lord_Howe`'s thirty-minute transition included. Materialization resolves **identity before authority**, so a decision naming an occurrence that already exists returns that occurrence and writes nothing, whatever has since happened to `enabled`, `config_revision` or `next_fire_at` — which is what makes a self-disabling one-time trigger safe to race — while authority is still applied in full to every new identity. Concurrent schedulers need no leader election, lease or coordination table: the write lock decides which proceeds and the database's own facts decide what the loser does. Admission backpressure rolls a candidate back whole and leaves the schedule due; a disabled Agent yields a `skipped` occurrence without ending a recurring schedule; an unevaluable schedule records one skip with a static reason and retires itself. **E2 added no migration** and **one dependency, `cronsim>=2.7,<3`**; the migration head remains `0008_stage_e1_trigger_scheduling`. It added no webhook or event ingress, no trigger management or occurrence-history API, and no frontend surface.

D2 adds the permission layer that will gate every future tool call, and it adds nothing that can call one. A user's Agent Instance holds an explicit ALLOW row per tool; there is no DENY row, so "not granted" and "revoked" are the same observable state and an entire class of precedence bug cannot be expressed. The call-time evaluator derives its authority from durable rows rather than from its caller: it takes only a Run id and a tool definition id, loads the Run to obtain the Agent Instance and the monotonic grant cutoff, loads the grant, and loads the definition to compare `grant.reviewed_fingerprint` against `tool_definitions.fingerprint` directly. No caller can supply an Agent, a cutoff, or a fingerprint, so a stale grant cannot be revived by presenting the fingerprint it was reviewed at. Revocation deletes the row and bites at the very next check; a new grant is invisible to a Run already submitted because its id exceeds that Run's snapshotted cutoff; re-granting and re-confirming both mint a new AUTOINCREMENT id, so the capability reaches only Runs submitted afterwards. Drift fails closed as `DEFINITION_CHANGED`, an unavailable definition as `DEFINITION_UNAVAILABLE`, a disabled MCP connection as `CONNECTION_DISABLED`, and a cross-owner MCP grant as `OWNER_MISMATCH` — the last re-proven at call time against the connection's owner rather than trusted from grant creation, so a grant row inserted directly into the database still fails closed. Annotations remain presentation-only and never grant. The evaluator is a pure read: it writes no `tool_invocations` row, emits no Run Event, and touches no execution state. **D2 added no migration and no dependency**; the migration head remains `0007_stage_d1_tool_capability_audit`. Nothing executes a tool yet: there is still no registry, no canonical schema validator, no built-in tool, no MCP client, no provider tool calling, and no Think → Act → Observe loop.

D3 makes a tool a durable, provider-neutral thing NervOS describes, validates and owns, and it makes exactly three of them real — while still letting nothing call one. A **canonical tool-schema subset** is defined and enforced by a NervOS-owned validator rather than a general-purpose JSON Schema library, so the subset and its enforcement cannot drift and no code path can be induced to resolve a `$ref`: unsupported constructs are rejected by name, never normalised and never silently weakened. Two validators sit over it — one answering *is this schema inside the subset*, the other *do these values conform* — and neither coerces a value, drops an unknown field, or applies a universal byte ceiling, because `Run.tool_result_max_bytes` remains D4's to enforce. The domain gains a **durable `ToolDescriptor`** whose id is always a real row id, so a descriptor without durable identity cannot be constructed, and a **JSON-safe value contract** that rejects `NaN`, `Infinity`, `Decimal`, `bytes`, `datetime`, tuples and arbitrary objects rather than implying that any Python value is supported. A **`ToolSource` / `ToolExecutor`** pair, registered in a **source-identity registry** keyed by `ToolSourceRef(source_kind, source_id)`, leaves room for several MCP connections beside the built-in source with no redesign. Model-facing names follow the frozen deterministic algorithm — lowercase, UTF-8 **byte-wise** escaping, and a hash suffix always appended — so the escape is injective and every name is provider-safe and at most 64 characters; the **definition fingerprint** is taken over canonical semantic JSON, so a cosmetic re-spelling cannot churn it. **Durable reconciliation** makes the built-in definitions idempotent: a repeat writes nothing at all, the persisted `model_name` is never recomputed or renamed, and a material change moves the fingerprint while leaving every `reviewed_fingerprint` byte-identical — so D2's accepted evaluator suspends the grant until the user re-confirms. The three frozen built-ins are `current_time` (an injected clock, UTC or a fixed `±HH:MM` offset), `calculate` (a bounded arithmetic grammar over `Decimal`, deliberately **not** `eval`) and `json_transform` (RFC 6901 selection and projection inside a bounded JSON document); all three are side-effect-free and credential-free, and none touches the filesystem, a subprocess, a shell or the network. **D3 added no migration and no dependency**; the migration head remains `0007_stage_d1_tool_capability_audit`. **Nothing executes a tool yet:** there is still no provider tool calling, no `ToolCall`, no `TOOL_USE`, no Think → Act → Observe loop, no `tool_invocations` execution write and no MCP client. D4 owns provider-neutral tool calling and the loop; reconciliation is complete and integration-tested but deliberately unwired, so D4 calls the finished service rather than redesigning it.

D4 makes the tool layer real without letting a provider decide anything. It adds **provider-neutral tool carriers** — a `ToolSchema` offered to a model, one canonical `ToolCall` whose arguments are always JSON **text** regardless of which adapter produced them, and conversation turns that carry an assistant turn's prose beside its calls and a tool result's structured content beside its text — so **no provider-shaped object ever escapes `nervos-models`**. Both adapters translate only their own local tool form: Anthropic `tool_use` / `tool_result` with parallel tool use disabled, and OpenAI Responses `function_call` / `function_call_output` with `store=False` unchanged; a hosted capability — shell, computer, code interpreter, MCP, web search — is still rejected, and a tool-free request stays exactly as strict as it was before D4 existed. A **new exact agent version `nervos.chat@2`** (`max_model_calls` 8, `max_tool_calls` 8) is registered beside an unchanged `nervos.chat@1`, which remains tool-free: its snapshot writes `max_tool_calls = 0` and `tool_grant_cutoff_id = 0`, so a Run of it never assembles a catalog, never records an invocation, and is byte-for-byte the Stage B/C execution it always was. A Run's tool limits and its **grant cutoff** are snapshotted inside the serialized submission transaction — the cutoff only for a Run that may actually use tools — so a capability granted after submission is invisible to it. Each Attempt assembles **one immutable grant-filtered catalog**, ordered by model-facing name and measured as canonical JSON over the whole array against 32 tools and 64 KiB, and an oversized catalog fails the Run as `tool_catalog_invalid` rather than being truncated. The **Think → Act → Observe loop** is NervOS's: it runs sequential model turns, executes calls **in provider order**, and consults D2's live predicate **twice** per call — once when the call is requested, and again inside the single transaction that commits `started`, which is the ambiguity boundary. A durable **ToolInvocation state machine** records `requested → denied | cancelled | started → succeeded | failed | ambiguous`, with the start boundary enforced by the insert itself rather than by its caller's discipline; `mark_started` re-runs the *same* extracted D2 predicate and commits exactly one branch atomically, so a grant revoked between the two commits becomes a denial with zero dispatches. **Multi-turn usage is crash-durable**: each model turn's reported tokens are merged and persisted to the Attempt before any tool may run, and a usage write that cannot commit stops the loop instead of performing an effect whose cost has no record. **Budgets belong to the Run and are checked before the work they bound** — every provider request including in-Attempt retries, the whole returned call batch preflighted so an over-budget turn dispatches nothing, and three consecutive failures. Only a rate limit is retried in place (1s/2s/4s, no jitter), and a rate limit that cannot be retried stays a rate limit rather than being relabelled a loop limit. Result evidence is one canonical envelope driving both digest and true byte count, with the model-visible observation bounded to `tool_result_max_bytes` including its own disclosure and structured JSON kept whole or omitted, never sliced; a post-start timeout is `ambiguous` and never retried; and a **C4 guard** inside the existing retry-decision transaction refuses to replay an Attempt that ever dispatched a call. All three D3 built-ins execute through the loop, and the three built-in definitions are reconciled at Worker startup — **after** the schema revision has been validated, so a Worker that refuses to start cannot have written. **D4 added no migration and no dependency**; the migration head remains `0007_stage_d1_tool_capability_audit`. **D4 emits no tool Run Events**, so the public tool timeline does not exist yet: `tool_invocations` is the durable internal truth, an unknown fabricated tool name is denied in memory and never dispatched, and the public audit projection for such a call waits for D6. Still **not** included: MCP of any kind, a capability frontend, the public tool audit/timeline projection, and the complete D6 crash, cancellation and timeout acceptance — including reconciliation of a lingering `started` row.

D5 connects the finished loop to real external tool sources without letting a remote server decide anything. `packages/nervos-mcp` becomes a real workspace member on the **official MCP Python SDK v2** (`mcp>=2.2,<3`, resolved `mcp 2.2.0`) and is the only place in the repository that imports it: `nervos-core` still imports no SDK, no route or application module names one, and only a composition root binds the port to the concrete implementation. **Only the modern `2026-07-28` revision is accepted, and that is proved rather than assumed**: every client is opened with the SDK's own `mode="auto"` negotiation and the negotiated `protocol_version` is inspected immediately after the connection is established and *before* any catalog request, so a server that negotiates anything older is closed and refused as `mcp_protocol_unsupported` with no `tools/list` ever attempted against it. There is no silent downgrade, and no refused remote version is ever interpolated into durable or public text, because every `McpError` message is looked up from a fixed table keyed by its code and there is no constructor that accepts one. Two transports are supported. **Streamable HTTP** runs through the SDK's own transport behind an **operator-owned origin allowlist** that is checked *and*, independently of it, a forbidden-address test that refuses loopback, RFC1918, link-local, multicast, unspecified, reserved, cloud-metadata literals and IPv4-mapped equivalents — the address test is applied before the allowlist can help, so a well-meaning operator cannot widen an allowlist into a private network, and same-origin-only redirect handling is the SDK's own rather than a second implementation. **Operator-declared stdio** starts a process the operator alone named: a connection row may carry only an opaque `server_key`, while the executable, argument vector, working directory and environment live in configuration the operator writes, so no field anywhere in the request schema can express a command, an argument vector, a shell, a working directory or an environment mapping. The child's environment is the operator's declared additions plus one credential and nothing else, and its stderr is discarded rather than inherited or logged. **Credential aliases** are operator-declared names bound to the targets they may be spent on: the durable row holds the alias and never a value, the binding is re-proved at the last practical moment before a transport is constructed, and a missing or unbound secret is a known pre-dispatch failure rather than a retried one. **Discovery is bounded rather than trusting.** One deadline covers connect, negotiation and every page; a repeated cursor fails the catalog and an independent page cap bounds a server that emits endless *distinct* cursors; a catalog exceeding 128 tools fails closed instead of being truncated, including the boundary case where exactly 128 arrive with a cursor still pending; duplicate upstream names fail it outright; and every schema NervOS is required to rely on — the input schema *or* an advertised output schema — must be inside D3's canonical subset, so a tool whose contract cannot be understood is stored as `unsupported_schema` and never offered, rather than being exposed with its declared shape quietly dropped. The raw remote schema is never persisted. **Reconciliation is atomic after the network.** The entire discovery phase completes outside any transaction, and only then does one `BEGIN IMMEDIATE` write the reconciled definitions and the connection's verdict together, so a crash cannot publish a `connected` connection whose catalog was not reconciled. Unchanged material writes nothing at all — not even a timestamp — a changed material moves the fingerprint while the persisted `model_name` and every `reviewed_fingerprint` stay byte-identical, a removed tool becomes `unavailable`, and one that reappears returns with its own fingerprint; drift therefore remains the fail-closed signal a user re-confirms, and nothing here ever writes a grant. Disabling sets `enabled=false` and the catalog to `disabled`, and re-enabling returns to `needs_refresh` while withdrawing the old definitions in the same transaction, so flipping a flag back cannot make a stale catalog executable. **D5 added no migration**: the five catalog states and the `unsupported_schema` status were already declared by `0007`, and the migration head remains `0007_stage_d1_tool_capability_audit`. Runtime integration is deliberately one narrow seam: a **`ToolSourceSynchronizer`** runs once before each Attempt assembles its catalog and re-synchronises the Worker's local registry from durable rows, opening no socket and issuing no `tools/list`, which is what makes a connection discovered a moment ago usable **without a Worker restart**. Catalog assembly now gathers every registered source, so a single Attempt may offer built-ins beside several MCP connections, and `McpGateway` caches live clients per connection with lazy ten-minute idle expiry, invalidating and closing them newest-first because the SDK's clients nest anyio cancel scopes that must unwind last-in-first-out. `McpToolSource` reads durable definitions only and `McpToolExecutor` maps one `tools/call` onto a `ToolResult`, a classified `ToolExecutionFailure`, or **`ToolOutcomeUnknown`** — the provider-neutral ambiguity signal D4 already modelled for a timeout, now reachable by an external executor, which records the invocation `ambiguous` and never retries or redispatches it. A server-declared `isError` is a *known* failure rather than ambiguity and its text is never repeated to the model, and a `structuredContent` is accepted only against an admitted output schema, where a non-conforming value is a classified failure rather than a silent drop. Remote annotations remain untrusted presentation metadata throughout and can neither grant authority nor authorize a replay. Acceptance drives the real chain — D2's live evaluator, the durable invocation state machine and the C4 replay guard included — against a **deterministic fake MCP server built on the official SDK** over both transports: `read_document` and `write_document` are proved end to end through the loop, and a `write_document` that dispatched exactly once is proved never to be redispatched when the following model turn stays rate-limited, with the ledger remaining at exactly one write and the whole-Attempt replay refused. **Owned by D6, and delivered there:** the public ToolInvocation audit surface, the tool-specific Run Event projection, reconciliation of a lingering `started` row, and the full C3/C5/C6 tool-interaction acceptance. No capability or audit frontend is included.

D6 makes the tool layer's *history* real without ever letting it authorize anything. The six `tool.*` Run Event types that `0007` had accepted since D1 finally become representable in the domain — the enum had lagged the schema, so reading one back raised — and every authoritative ToolInvocation transition now appends its matching event **inside the same short transaction** as the transition itself, through one shared append primitive that both the tool lifecycle and the Job/Attempt lifecycle write through. An event therefore commits with its cause or not at all, and the compare-and-set that makes a repeated transition a no-op is what makes a repeated event impossible; a second terminal write rolls back rather than recording a second fact. **`tool_invocations` remains the authority.** Run Events are an append-only, safe, chronological projection that never decides permission, grants, retry, replay, definition availability or execution authority, and an architecture guard keeps every deciding module from reading the table at all.

**The public timeline gained one opaque field.** `run_events` already carried a nullable `tool_invocation_id`, and `RunEventResponse` now projects it — an identifier that names no tool, argument, result, digest or connection, so the existing C7 endpoint publishes *that a call happened* without publishing what it carried. No public ToolInvocation endpoint was added, and no argument, result, digest, credential, provider error or server text can reach the timeline: D6's append call sites derive every code and message from the reviewed static allowlist, and a marker test proves an argument value and a remote result text appear nowhere in durable audit. **Refusals that never became a row are audited too.** An unknown tool name, malformed arguments and arguments a canonical schema rejected are now recorded as one `tool.denied` with a NULL link and the single generic public code `tool_denied`, because those calls are real facts about what a model asked for and inventing a sentinel definition, a fake id or another Run's id for them would make the durable record assert something untrue. The precise internal `denied_*` reason stays on the invocation row, as ADR 0015 requires. **Audit precedes the observation**: if the refusal cannot be committed because the Run was cancelled or the Worker lost its claim, the loop stops instead of narrating a refusal the timeline never recorded.

**Cancellation, recovery and late results now agree about tools.** A `requested` invocation abandoned by a Worker loss or a cancellation is closed `cancelled` — the frozen state meaning "stopped before it ran" — with no tool event, because the six frozen types deliberately contain no `tool.cancelled` and `run.cancelled` remains the cancellation truth. A `started` one may already have reached an external system, so it is closed `ambiguous` with `tool_outcome_unknown` and one `tool.ambiguous` event, never `failed`. Both reconciliations run inside the transaction that already owns the Job/Attempt truth, bounded by `attempt_id`, so there is no window in which the Attempt is terminal and a call it dispatched is still `started`, and no periodic sweep exists anywhere. Event ordering is frozen and asserted: recovery reads `attempt.expired → recovery.ambiguous → tool.ambiguous → run.failed`, and cancellation reads `cancellation.requested → tool.ambiguous → run.cancelled`. Late model and late tool results stay fenced by the existing claim, status and lease compare-and-sets, so nothing resurrects a cancelled Run, and **C4's dispatched predicate is unchanged** — no replay path was added and no retry policy was inferred from the new status names. D6 makes **no exactly-once claim**: it prevents known unsafe replay after dispatch and records ambiguity when a remote outcome cannot be proven.

**The tool layer was integrated with Stage C rather than beside it.** A multi-turn Think → Act → Observe loop is still one Run, one Job and one Attempt — no tool queue, no continuation job, no job per turn — and the Attempt holds its concurrency slot and its claim for the whole loop, which is why a long MCP call is safe: lease renewal runs as an independent task and is never starved by the awaiting Attempt. C6 concurrency, fairness and backpressure semantics are untouched and their suites pass unchanged. Two pre-existing MCP client-lifecycle defects were found by D6's own acceptance and fixed narrowly in `nervos-mcp`: concurrent cold callers no longer each open a connection (one stable creation lock per connection, with the pending outcome shared so a cancelled caller cannot start a second), and each live connection's client context is now entered **and exited by one dedicated lifecycle-owner task**, so a session created inside an Attempt can be closed by the Worker's shutdown task — previously any Worker that had run a single MCP call raised on shutdown, because an anyio cancel scope cannot be exited from another task. Warm-session callers still use the live client concurrently and directly; nothing is serialised by either fix.

**D6 added no migration** — every state it reconciles was already representable in `0007`, whose head is unchanged. The only frontend change is compatibility repair: the Run timeline validates its event types from a runtime allow-list with no fallback, so the six new literals had to be taught to the existing union, allow-list and copy map or the whole page would fail validation for any tool-using Run. **Not included:** Stage F checkpoint/resume, exactly-once remote execution, a public ToolInvocation endpoint, a capability-management UI, Stage H approvals and sandboxing, and the D7 closeout.
Stage C — Persistent execution engine is complete. C0–C8 are all implemented, externally reviewed, and accepted. The C0 architecture freeze is complete. C1 passed external implementation and remediation review, was finalized as implementation commit `8e9c9da`, and was merged to `main` in merge commit `6d54eac`. C7 and C8 were planned and implemented as one combined Stage C delivery and are recorded in ADR 0014 and the C7/C8 sections above.

Stage B implementation is complete. B4 passed external implementation review and hosted checks, was finalized as implementation commit `faa52a2`, and was merged to `main` by pull request #8 in merge commit `acb55b3`.

No live provider proof has been executed for either provider; those optional proofs remain separately authorized and non-blocking.

## D7 implementation verification

D7 is integrated deterministic acceptance for Stage D and its closeout, not a feature milestone. It adds **no** product architecture: no API, no route, no status, no table, no migration, no execution policy, no queue policy, no transport, no configuration, and no authority mechanism. **No production source file changed at all.** No production defect was found by the integrated journeys, so no source re-freeze and no evidence rerun was required.

**The composition was the point.** Every pre-D7 test in the repository constructed `RunExecutor` **without** a `tool_loop`; only the Worker composition root ever wired one. The routing decision inside `run_execution.py` — which execution shape a Run takes, and what happens when a tool-enabled Run arrives with no claim to fence its writes on — had therefore never been exercised by a test. D7's harness builds the composition the Worker builds, and every journey is driven through the real `JobExecutionService` → `RunExecutor` → `ToolLoop` path rather than calling `ToolLoop.run` directly. That fail-closed branch is now covered too: a tool-enabled Run reaching the executor without a claim fails as a normalized internal error with zero provider calls, instead of silently falling back to the tool-free path.

**Integrated journeys.** Backward compatibility (`nervos.chat@1` is byte-for-byte the Stage B/C execution it always was, with no invocation and no tool event); the builtin `nervos.chat@2` Think → Act → Observe loop; one Attempt executing a builtin **plus two MCP sources** out of one frozen catalog with source-correct identities and per-source ledgers; MCP over Streamable HTTP and over an operator-declared stdio `server_key` (a real child process, never a shell, with the child confirmed gone after shutdown); default deny; the Run grant cutoff across two Runs; revocation before the start boundary; fingerprint drift; `unsupported_schema` failing closed; a connection disabled **after** catalog assembly; model rate-limit after a remote write; Worker loss after a remote write; cancellation on both sides of the dispatch boundary; tool timeout; unknown and malformed requests; the consecutive-failure cap; the one Run / one Job / one Attempt shape; C6 concurrency, fairness and backpressure over a tool-enabled fleet; lease heartbeat across a multi-turn loop; lease loss with a late remote result; the MCP lifecycle-owner repair through the production `close_worker` consumer; restart persistence; a fresh database migrated to `0007` with a real Worker boot and clean shutdown; owner-scoped API behaviour; and the safe public tool timeline read through the C7 projection rather than raw SQL.

**No-replay acceptance.** Two journeys carry the milestone's most important claim, and both assert exact equality rather than a bound: a rate limit following a remote `write_document` leaves the fake server's ledger at **exactly one** entry with `attempt_count` 1 and no `retry.scheduled`, and an expired lease following a remote write leaves the ledger at **exactly one** entry and closes the invocation `started → ambiguous` with the frozen event tail `attempt.expired → recovery.ambiguous → tool.ambiguous → run.failed`. Cancellation after dispatch releases the held remote call *after* the Run is cancelled, proving the late result is fenced and cannot resurrect it.

**Audit, privacy and the browser.** The tool lifecycle is asserted through the public read path, with monotonic sequences and correct invocation linkage, and with no argument, result, digest, provider error or internal-ambiguity text reachable in any published field. The deterministic supervised browser journey gained **one** seeded tool-enabled Run and asserts only that the Run is visible, the tool lifecycle renders, the final state settles, and no raw or internal material reaches the DOM — all pre-existing Stage A–C5 assertions unchanged.

**D7 added no migration and no dependency**; at D7 the migration head was `0007_stage_d1_tool_capability_audit`, byte-identical throughout, and `0008` did not yet exist. (Stage E's E1 later consumed `0008` for the trigger schema.) **Not included:** the Stage E scheduler, events and triggers; Stage F conversation sessions and memory; the Stage G package system; Stage H approvals, sandboxing and isolation; and any exactly-once remote-effect claim.

**Stage D proves capable tool-using agents.** It does not yet provide scheduling, background triggers, persistent conversation memory, installable agent packages, or interactive approvals and isolation — those remain Stages E, F, G and H. NervOS still makes **no exactly-once claim** for remote effects: it prevents known unsafe replay after dispatch and records ambiguity when a remote outcome cannot be proven.

## Maintenance rule

Update this file after every accepted milestone. Do not check an item merely because code was generated; verify its acceptance criteria first.
