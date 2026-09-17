# ADR 0015 — Tool Runtime, Capability Grants, and Audit Authority

Status: Proposed — D0 architecture freeze, pending external review

This ADR freezes the policy half of Stage D: what a capability *is*, who may hold one, when it is
checked, what may be recorded about a call, and what may never be. The protocol half is ADR 0016; the
durability half is ADR 0017.

## Context

Stage C produced a durable execution kernel that runs exactly one model call per Attempt. It is
authoritative about *whether* a Run may execute (a live Job lease and a fenced Attempt token) and
about *whether* a failed execution may be replayed (one positively-safe outcome code). It has no
notion of a tool, no notion of a capability, and no notion of a per-Agent authority.

Three facts about the merged repository shape everything below.

**The model port cannot express a tool.** `ModelRequest` carries no tools and `ModelResponse` carries
no tool calls; both adapters actively *reject* provider tool output as `MODEL_RESPONSE_INVALID`.
Stage D widens that surface deliberately and must keep the rejection path for everything it does not
recognize.

**The repository already froze the intended architecture in prose.** `docs/permissions.md` requires
granular capability names, states that *"Risk classification is metadata; actual user grants remain
explicit"*, that *"Tool connection != agent permission"*, and that *"Permission to use a capability
must not imply access to the raw credential."* `.claude/skills/add-mcp-tool/SKILL.md` fixes the flow
`Agent -> Tool API -> Permission Engine -> MCP Gateway -> Tool`. This ADR implements those
requirements; it does not reinterpret them.

**The roadmap assigns "capability permissions" to both Stage D and Stage H.** Left unresolved, that
overlap either grows D into H or leaves D unusable. §Boundary below resolves it.

## Decision

### Terminology, fixed so the words cannot drift

| Term | Meaning | Durable? |
|---|---|---|
| **Tool Definition** | One callable operation exposed by one source, with a canonical name, a schema, and a fingerprint. `github/create_issue`. | yes — `tool_definitions` |
| **Capability** | The *authority* to invoke one Definition from one Agent Instance. A relationship, not a row. | no — derived |
| **Grant** | The durable record that materializes a Capability: `(agent_instance_id, tool_definition_id)`. **Its existence is the authority.** | yes — `agent_tool_grants` |
| **Risk class** | Informational classification derived from source annotations. Never authority. | metadata on the Definition |
| **Permission decision** | The call-time allow/deny verdict, evaluated fresh for every call. | recorded on the ToolInvocation |

`docs/permissions.md`'s `gmail.read` / `gmail.send` are therefore **Tool Definitions**, and "capability"
is what an Agent Instance holds.

### Ownership

```
users
 ├─ owns  mcp_connections           (approved tool sources)
 │         └─ exposes tool_definitions
 ├─ owns  agent_instances
 │         └─ holds agent_tool_grants
 └─ owns  runs
           └─ owns tool_invocations
```

A connection has exactly one owner, set at creation and never reassigned — the immutability rule
`agent_instances.owner_user_id` already has. A grant may only bind an Instance and a Definition
sharing one owner, re-checked **inside the grant transaction** rather than pre-flighted. An
invocation is reachable only through its Run, through the proven owner-scoped read. Foreign and
missing are indistinguishable everywhere.

**One Agent's grant implies nothing for another.** A shared connection is a shared *source of tools*,
never a shared *authority*.

### Precedence: ALLOW-only, with no DENY row

```
1. Hard runtime/security denial    (non-overridable; no row can grant it away)
2. Absence of a grant              (default deny)
3. Presence of an explicit grant   (allow)
```

**There is no DENY row in Stage D.** Deny is the absence of a grant. This removes an entire class of
precedence bug and makes "never granted" and "revoked" the same observable state. A future milestone
that needs deny-lists can add a rung above the grant without disturbing this one.

Annotations never appear in this list. A drifted definition fails closed. A tool absent from the
Run's presented catalog is denied and audited, never executed.

### The grant table is current authority, not a change ledger

**`agent_tool_grants` records the authority that exists now.** It is not, and must not be described
as, a complete historical ledger of every grant and revocation: revoking deletes the row, and a
deleted row cannot be reconstructed from what remains.

What Stage D *does* record durably is the **permission decision used for each actual ToolInvocation**
— so "was this call allowed?" is answerable after the fact for every call that happened. Historical
grant-*administration* audit (who granted what, when, and why) is not provided by Stage D and is
explicitly deferred to Stage H or another security milestone. Stage D does not add a second
permission-change table to approximate it, because two records of the same authority can disagree.

**No claim of the form "every historical grant/revoke action can be reconstructed from the current
grant rows" may appear in Stage D documentation or UI copy.**

### The Run-scoped catalog: a monotonic grant cutoff, never a clock

A first formulation froze the catalog **per Attempt**. That is wrong: C4's only replay path creates a
**second Attempt inside the same Run**, and because Stage D persists no conversation state, a second
Attempt would rebuild its catalog from live grants. A tool granted *while the Run was running* would
then become callable inside work the user had already started and reviewed — precisely the capability
expansion the frozen contract forbids.

A second formulation compared `grant.created_at <= run.created_at`. **That is also rejected.** A
security boundary must not rest on a wall-clock comparison: two rows written in the same millisecond,
a clock adjustment, or a future non-local store would each make the rule ambiguous, and C6 already
refused clock-based authority for fairness scheduling for exactly this reason. **What is needed is a
durable monotonic cutoff, not a timestamp.**

**Frozen: `agent_tool_grants.id` is the ordering primitive.**

It is an `INTEGER PRIMARY KEY AUTOINCREMENT` — monotonic, and never reused within a database. At Run
submission, inside the serialized submission transaction, the highest grant id currently visible to
that Agent Instance is snapshotted onto the Run:

```
runs.tool_grant_cutoff_id = max(grant.id where grant.agent_instance_id = <this instance>)   -- 0 if none
```

A grant may be offered to a Run **only when all of the following hold**:

```
g.agent_instance_id = R.agent_instance_id
g.id                <= R.tool_grant_cutoff_id     -- the durable cutoff
g still exists                                    -- revocation deletes the row
g.reviewed_fingerprint matches the definition currently presented
the definition is available and has not drifted
the connection is enabled
every current hard runtime policy check passes
```

Because the id is monotonic and never reused, the two directions fall out exactly as required:

- **Revocation is immediate.** Deleting the row removes it from the set, so an existing Run loses the
  authority at its very next call-time check.
- **A new grant is invisible to an existing Run.** Its id is strictly greater than that Run's cutoff,
  so no Attempt of that Run can see it — including a retry Attempt.

**No timestamp decides authority anywhere in this rule.** `created_at` remains on the row and is audit
and display metadata only.

### Re-granting and re-confirming mint a new identity

This is what makes the cutoff meaningful across a definition change, and it closes a subtle hole:

> A Run starts → the MCP definition materially changes → the user reviews and re-confirms it →
> the **existing** Run must not acquire the changed definition.

The hole exists because an in-place update of `reviewed_fingerprint` on the old row would leave its id
unchanged, and that id is already at or below the Run's cutoff — so the Run would silently gain a
capability it was never started with.

**Frozen: both operations replace the row rather than editing it.** In one serialized, owner-scoped
transaction:

- **Re-confirming a drifted definition**: delete the previous grant row and insert a **new** row with a
  **new AUTOINCREMENT id** carrying the newly reviewed fingerprint.
- **Re-granting after a revocation**: insert a **new** row with a **new id**.

In both cases the new id is strictly greater than any existing Run's cutoff, so the capability becomes
visible only to Runs submitted afterwards. **A grant row's `reviewed_fingerprint` is therefore
immutable for the life of that row**, which also removes an entire class of in-place mutation bug.

### Catalog snapshot: what Stage D does and does not promise

An earlier draft implied the exact catalog presented to every Attempt could always be reconstructed
later. **That is not true and must not be claimed.** Stage D deliberately persists no conversation
history and no catalog snapshot table, so the concrete catalog an Attempt assembled exists only in
Worker memory for that Attempt's lifetime.

**The honest contract, frozen:**

- **Eligibility is durably bounded.** Which grants *may* be offered to a Run is fixed by
  `runs.tool_grant_cutoff_id` and is fully reconstructible from durable rows.
- **The concrete assembled catalog is not durable.** It is fixed in Worker memory for one Attempt and
  is not a durable artifact.
- **Each actual ToolInvocation snapshots the exact identity and fingerprint it acted on.** So the
  audit answers "which tool, under which reviewed definition, was actually called?" — which is the
  question that matters — without reconstructing the catalog that offered it.
- **Call-time authority is live**, re-read from durable rows immediately before dispatch.
- **Full durable conversation or catalog replay is not a Stage D feature** and belongs with later
  durable-session or checkpoint work.

**The catalog snapshot never grants authority.** It bounds what may be asked; the live rows decide
what may be done.

### The catalog snapshot and live authority are different things

| Concern | Nature | Why |
|---|---|---|
| The catalog presented to the model | **In Worker memory for one Attempt only — not durable** | The model must not gain tools mid-flight; reconstructing it later is not a Stage D feature (see above) |
| Each tool's reviewed fingerprint | **Snapshot on the grant** | This is what "the user approved *this* definition" means |
| The right to invoke, right now | **Live, at call time** | The point of revocation |

**The forbidden inference, named so no implementation can make it:** *"it was allowed when the Run
was submitted" is never a reason to allow a call now.*

The split is precise, and it is deliberately asymmetric: **eligibility is durable** (bounded by
`tool_grant_cutoff_id`, reconstructible from rows), the **assembled catalog is not** (Worker memory,
one Attempt), and **authority is live** (re-read immediately before dispatch). None of the three may
be treated as permission for the next call — only the live check decides that.

### The two-transaction call boundary

Authorization is checked **twice**, and the second check is the one that binds.

The first check runs in the loop, before anything is committed. It is necessary but not sufficient,
because two durable commits (`requested`, then `started`) separate it from the network call, and a
revocation landing in that window must not be lost.

**The second check is frozen as an exact predicate.** Immediately before the ToolInvocation moves
`requested → started`, inside one serialized, fenced transaction, **all** of the following are
re-read from durable rows and must hold:

| # | Re-checked |
|---|---|
| 1 | the Run, Job and Attempt are still the current authority (the same fenced predicates every other Stage C write uses) |
| 2 | the owner relationship still holds between Instance, Definition and Connection |
| 3 | the grant row **still exists** |
| 4 | `grant.id <= run.tool_grant_cutoff_id` |
| 5 | the grant's `reviewed_fingerprint` matches the descriptor fingerprint the Attempt presented |
| 6 | the definition is still available |
| 7 | the definition has not drifted |
| 8 | the connection is still enabled |
| 9 | the credential alias is still valid **for this target** |
| 10 | the operator's stdio/HTTP trust policy still permits this target |

**Only if every one passes** may the transition be written and committed. **External dispatch may
happen only after that commit.** If any check fails, **no dispatch occurs** and the appropriate
terminal truth — `denied`, `cancelled`, or a classified safe failure — is persisted instead.

**No database transaction spans MCP or tool execution.** The dispatch sits between two short
transactions, never inside one.

Between the `started` commit and the actual dispatch there remains an in-process gap that no
transaction can close. That is why the contract is stated as **"effective before the next dispatch"**
and never as "effective at every instant", and why an already-dispatched call is explicitly
irreversible (ADR 0017).

### Definition identity, fingerprint, and drift

Durable identity is `(source_kind, source_id, upstream_name)` — a name alone is not unique, because
two servers may both expose `read_file`.

**SQLite does not enforce uniqueness over a nullable column**, so the built-in case needs a different
construction than a plain three-column `UNIQUE`:

```sql
CHECK (  (source_kind = 'builtin' AND source_id IS NULL)
      OR (source_kind = 'mcp'     AND source_id IS NOT NULL) )

CREATE UNIQUE INDEX uq_tool_definitions_builtin
    ON tool_definitions (upstream_name) WHERE source_kind = 'builtin';
CREATE UNIQUE INDEX uq_tool_definitions_mcp
    ON tool_definitions (source_id, upstream_name) WHERE source_kind = 'mcp';
```

Both are **partial unique indexes**, which SQLite enforces correctly. `UNIQUE(model_name)` remains a
separate global guard. D1's migration tests must prove that a **duplicate built-in identity is
rejected**, since that is the case the naive constraint would have missed.

The **fingerprint** covers everything a model or a user materially reviewed:

```
sha256(canonical_json({
    model_name, upstream_name, description,
    input_schema, output_schema,
    source_kind, source_id,
    risk metadata displayed to the user        # annotation-derived
}))
```

Annotations are included so that a server changing the risk metadata a user previously reviewed
**suspends the grant**. Including them does **not** make them trusted: an annotation is still
presentation-only (below). Metadata with no bearing on capability or risk — cache expiry, discovery
timestamps — is deliberately **excluded**, so ordinary server housekeeping does not churn every grant.

On a material mismatch the grant becomes **ineffective immediately**: the tool is listed as
**REVIEW REQUIRED**, a call fails closed, and no dispatch occurs until the user re-confirms and the
new fingerprint is stored. A tool removed upstream keeps its history, is marked unavailable, and its
grant is suspended. A tool renamed upstream is a **remove plus an add** — a new durable identity,
with **no inherited grant**.

### Annotations are untrusted hints

`readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint` come from the server, and a
server can lie. They may influence **presentation only** — a badge, ordering, a warning level. They
may **never** grant authority, remove authority, permit automatic retry, or satisfy any predicate in
the permission evaluator or the replay guard.

This is not merely a NervOS preference. The MCP `2026-07-28` specification states that clients
**MUST consider tool annotations untrusted unless they come from trusted servers**, and that every
annotation property is a hint rather than a guarantee. Its own defaults are already the conservative
reading, and Stage D adopts them verbatim:

| Annotation | Spec default | Reading when absent |
|---|---|---|
| `readOnlyHint` | `false` | not read-only |
| `destructiveHint` | **`true`** | possibly destructive |
| `idempotentHint` | `false` | not idempotent |
| `openWorldHint` | **`true`** | possibly open-world |

### The canonical tool schema

Supported: an object root with `properties`, optional `required`, optional
`additionalProperties: false`; `string`, `number`, `integer`, `boolean`; arrays of any supported type;
nested objects to a bounded depth; `enum` of primitives; `description` on any node.

**Rejected outright, never normalised:** `$ref`, `oneOf`, `anyOf`, `allOf`, `not`, `if`/`then`/`else`,
`patternProperties`, `dependentSchemas`, and any unnamed keyword.

This is a **disclosed narrowing**: the specification says clients *SHOULD* follow its `$ref`
resolution requirements, and Stage D knowingly does not. The alternative is a resolver whose
correctness is a permanent obligation and whose input is untrusted. A tool using `$ref` is listed as
`unsupported_schema` with the reason shown, so the user sees a precise limitation instead of a tool
that behaves differently than advertised. `outputSchema` is JSON Schema 2020-12 at the source; Stage D
admits the same subset.

Validation is performed by a **NervOS-owned validator over this subset**, not a general-purpose
library, so that the subset definition and its enforcement cannot drift and so that no code path can
be induced to resolve a reference. **Arguments are always re-validated inside NervOS immediately
before dispatch**, regardless of what the model emitted or what a provider validated.

### Model-facing names

Provider tool names are constrained — Anthropic's must match `^[a-zA-Z0-9_-]{1,128}$` and OpenAI's
must be at most **64** characters — while MCP permits `.` and `/` up to 128. **Stage D targets 64,
the stricter bound**, so one name is valid for both providers with no per-provider branch. A raw MCP
name is therefore not reliably provider-safe.

```
nervos__builtin__<esc(upstream_name)>
nervos__c<connection_id>__<esc(upstream_name)>

esc(x)  lowercase, then per character:
          'a'-'z' | '0'-'9' | '-'   ->  itself
          '_'                       ->  '_5f'
          anything else             ->  '_' + 2 hex digits of the code point
```

`esc` is injective, so distinct upstream names cannot collide. Length is bounded by reserving the
fixed parts **before** truncating the variable one:

```
FIXED  = "nervos__" + ("builtin" | "c" + str(connection_id)) + "__"   # never truncated
SUFFIX = "_" + sha256(<the UNTRUNCATED composed name>)[:12]           # 48 bits, never truncated
BUDGET = 64 - len(FIXED) - len(SUFFIX)

assert BUDGET >= 8
model_name = FIXED + esc(upstream_name)[:BUDGET] + SUFFIX
```

**The connection's display name does not appear in the model-facing name.** An earlier formulation
embedded it and could therefore emit 140-character names, over any provider's limit; the human label
lives in `display_name` on the definition row, which is what the UI renders.

The name is **persisted once and never recomputed**, and connection ids are `sqlite_autoincrement`,
so a deleted-and-recreated connection receives a *new* id rather than a reused one — a recreated
connection is a genuinely new durable identity, and its grants do not survive it. `UNIQUE(model_name)`
remains the final fail-closed collision guard.

### Built-in tool sources: no Connection, and checks 8–10 do not apply

The dispatch predicate above is written for a source that has a Connection, a credential alias and an
operator trust target. **A built-in tool has none of those**, and an implementer reading the predicate
literally could conclude that every built-in call must fail, which would make Stage D's simplest and
safest capability nominal.

**Frozen:** a built-in definition has `source_kind = 'builtin'` and `source_id IS NULL`, and
**belongs to no `mcp_connections` row at all**. It is resolved by the registry, bound by a grant like
any other definition, and executed in-process by a `BuiltinToolExecutor` — the second implementation
of the `ToolExecutor` port, alongside the MCP one. Nothing about the loop, the grant model, the
cutoff, the audit, or the invocation state machine differs between the two.

Consequently, for a built-in definition:

| Predicate | Built-in |
|---|---|
| 1–7 (authority, ownership, grant exists, cutoff, fingerprint, availability, drift) | **apply unchanged** |
| 8 — connection still enabled | **vacuously satisfied** — no connection exists |
| 9 — credential alias valid for this target | **vacuously satisfied** — no alias exists |
| 10 — operator trust policy permits this target | **vacuously satisfied** — no network target exists |

The **first built-in tools**, which are what makes point 1 of D's usability demonstrable rather than
asserted: `nervos__builtin__current_time`, `nervos__builtin__calculate` (a bounded arithmetic grammar,
**not** `eval`), and `nervos__builtin__json_transform`. All three are deterministic, side-effect-free
and credential-free — chosen precisely so the architecture is proven before any remote source exists.
**No filesystem, shell, HTTP or credentialed tool is built in Stage D**; each would either need
Stage H's isolation or would smuggle Stage H work into the first demo.

**Built-ins carry a stronger guarantee than MCP tools, stated narrowly.** Because NervOS owns their
code, a built-in *may* declare an explicit, code-level idempotency invariant. **That guarantee is
never generalised to third-party MCP tools** — where it exists it is proof that the mechanism can
exist, not a template for trusting a server.

**Honest out-of-the-box statement, which the UI and runtime documentation must carry:** on a fresh
install the operator allowlist is empty and no stdio server is declared, so **only built-in tools are
reachable until an operator provisions a source**. That is the intended, fail-closed default rather
than a defect, and it is exactly the property that keeps Stage D usable without Stage H.

### What a ToolInvocation records, and what it may never record

| Recorded | Never recorded |
|---|---|
| `run_id`, `job_id`, `attempt_id`, `tool_sequence` (1..N per Attempt — the ordering authority, never a timestamp) | credential, credential alias value, access token, authorization header |
| definition identity **snapshot** (`source_kind`, `source_id`, `upstream_name`, `model_name`) so audit survives deletion | raw arguments |
| the reviewed `definition_fingerprint` **as presented for this call** | raw results |
| `status`, `permission_decision` | provider payloads |
| `provider_call_id`, bounded and sanitised, for continuation only | claim token |
| `requested_at`, `started_at` (the ambiguity boundary), `finished_at` | any other secret |
| `arguments_digest`, `arguments_shape` | |
| `result_digest`, `result_bytes`, `result_truncated` | |
| the safe normalized `error_code` / `error_message` pair | |

There is no column that could hold a secret — the same structural property `run_events` already has.

**Arguments and results are not persisted.** They routinely carry PII, and there is no encryption
until Stage H; persisting them would be exactly the "store secrets to make the audit prettier"
failure. What *is* stored is the digest, the byte size, a truncation flag, and an
**`arguments_shape`** — a bounded skeleton naming the top-level keys and their JSON types, with no
values, so an operator can debug *which fields did the model send?* without the values becoming
durable. The model still sees the real values; they are simply not written down.

**Stated limitation:** the audit answers *which tool, when, allowed, started, finished, outcome, size,
digest* — and not *with what arguments*.

### Digests are content evidence, not invocation identity

Argument and result digests prove that two pieces of canonicalised material are equal. **They prove
nothing else.** Two entirely legitimate tool calls may share identical arguments and identical
results, so a digest is **never**:

- evidence of idempotency;
- an exactly-once mechanism;
- a deduplication key;
- a ToolInvocation identity;
- a reason to suppress or replay a call.

**Invocation identity is `ToolInvocation.id`**, and its ordering within the Attempt is
`tool_sequence`. No write path may use a digest to decide whether a call should happen.

### Permission mutation

| Event during an active Run | Effect |
|---|---|
| **Grant revoked** | **Immediate**, at the next call-time check. An undispatched call is denied. A dispatched call cannot be un-dispatched; the audit says so. |
| **Grant added** | **Not visible to a running Run** — enforced by the Run-scoped catalog above, including across a retry Attempt. Visible from the next Run. |
| **Connection disabled** | Like revoking every grant it contributed, at the next check. |
| **Definition drifted** | Immediate: fails closed at the next check until re-confirmed. |

The revoke/add asymmetry is deliberate and points the safe way: revocation is a security action and
must bite immediately; addition is a capability expansion and must not silently appear inside work
already reviewed and started.

### Connection disable and delete

**Disable is the operation for stopping future dispatch, and it is always available.** It takes effect
at the next live permission check and leaves every historical row untouched.

**Hard deletion is configuration destruction, and it must not mutate structural dependencies
underneath live execution.** It is therefore refused with a safe `409` — and the owner is directed to
disable instead — whenever **either** condition holds:

- **A. History exists.** Any ToolInvocation references a definition from that connection.
- **B. Live work may still reference it.** Any nonterminal Run belonging to the owner may still
  reference one of the connection's grant or catalog identities. This is the condition an earlier
  draft omitted: a Run that is still executing can hold the connection in its capability set, and
  removing the rows underneath it would change the meaning of work already in flight.

Only a connection that is **never used and not live** may be physically deleted, and then in a single
`BEGIN IMMEDIATE`, owner-scoped transaction that removes its grants, its definitions, and the
connection together.

**Historical definitions and connections are effectively retained rather than physically deleted**,
carried as `disabled` / `unavailable` state. `ON DELETE RESTRICT` on both foreign keys makes this
structural rather than procedural, and `tool_invocations` carries its own identity snapshot so a row
stays readable and self-describing regardless. **Audit is never cascaded away.**

### Usage accounting

A tool-using Attempt's usage is the **sum of every model turn in that Attempt**, not the final turn's
values alone. The running total is written at each durable loop boundary so that a mid-loop Worker
loss does not silently discard the whole Attempt's accounted usage — today a single call is at stake;
in Stage D, many. The Run continues to carry the terminal Attempt's values, unchanged.

**This is aggregate accounting, not durable turn history**, and the distinction is stated so that no
reader conflates the two. Three summed integers reconstruct no turn, no argument and no result, and
enable no resume. The columns exist so that cost is not under-reported; they do not exist so that a
conversation can be rebuilt, and nothing may treat them as conversation state.

### The 0007 schema (frozen; not created in D0)

**Four new tables** — `mcp_connections`, `tool_definitions`, `agent_tool_grants`, `tool_invocations`
— with the partial unique indexes above, `UNIQUE(agent_instance_id, tool_definition_id)` and
`UNIQUE(attempt_id, tool_sequence)` respectively, `ON DELETE RESTRICT` on every audit-bearing
foreign key, and `INDEX(attempt_id, tool_sequence)`, which also serves ADR 0017's replay predicate.

**`agent_tool_grants`, frozen exactly:**

| Column | Notes |
|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` — **grant identity *and* the monotonic Run-cutoff ordering primitive** |
| `agent_instance_id` | FK, `ON DELETE RESTRICT` |
| `tool_definition_id` | FK, `ON DELETE RESTRICT` |
| `reviewed_fingerprint` | the definition fingerprint the user reviewed; **immutable for the life of the row** |
| `created_at` | audit and display metadata **only** — it decides no authority |

with `UNIQUE(agent_instance_id, tool_definition_id)`.

**Ids are never reused.** Re-confirming a drifted definition and re-granting after a revocation each
**replace the row** with a new AUTOINCREMENT id, so a capability can never be silently acquired by a
Run whose cutoff predates the review.

**`runs` gains five Stage D columns**, not four:

| Column | Meaning | Value for a migrated / pre-D Run |
|---|---|---|
| `max_tool_calls` | loop bound | **0** |
| `tool_timeout_ms` | per-call deadline | 30000 (the column default) |
| `tool_result_max_bytes` | result bound | 65536 (the column default) |
| `max_consecutive_tool_failures` | loop-safety constant | 3 (the column default) |
| `tool_grant_cutoff_id` | **the monotonic grant cutoff** | **0** |

`max_tool_calls = 0` **and** `tool_grant_cutoff_id = 0` together mean a migrated Run can never
accidentally acquire a Stage D capability: no cutoff admits any grant, and no tool call budget exists.
**No grants are inferred for historical Runs.**

**`job_attempts` gains three nullable usage columns** (`input_tokens`, `output_tokens`,
`total_tokens`) for multi-turn, crash-durable accounting. **`run_events` gains a nullable
`tool_invocation_id`** and an extended event-type CHECK.

`lifecycle_shape`, `limits_positive`'s existing clauses, and every other accepted constraint keep
their current meaning.

### The 0007 → 0006 downgrade preflight

**The downgrade refuses rather than destroys.** Dropping populated Stage D state would destroy durable
evidence — including the per-Attempt token accounting, which has no pre-0007 representation at all —
so the migration **preflights before any destructive schema reconstruction** and refuses if any
Stage D-only state is present.

The preflight passes only when **all** of these hold:

| # | Condition |
|---|---|
| 1 | `mcp_connections` is empty |
| 2 | `tool_definitions` is empty |
| 3 | `agent_tool_grants` is empty |
| 4 | `tool_invocations` is empty |
| 5 | no `run_events` row carries a Stage D tool event type |
| 6 | every `run_events.tool_invocation_id IS NULL` |
| 7 | every Run's four Stage D limits are at the exact legacy defaults above, and `tool_grant_cutoff_id = 0` |
| 8 | **`job_attempts.input_tokens`, `output_tokens` and `total_tokens` are all `NULL`** |

Condition 8 is the one an earlier draft omitted; it is the reason multi-turn accounting is listed
explicitly rather than folded into "no Stage D state".

If any condition fails, the downgrade **refuses clearly and leaves the database logically unchanged**
apart from unavoidable transaction metadata. It never partially downgrades, never deletes history to
succeed, and never rewrites a Run Event or a ToolInvocation. D1's migration tests must prove all three
properties: an empty Stage D schema downgrades, a populated one refuses, and a refused downgrade is
atomic with the data intact.

This follows the principle the earlier lifecycle migrations already established: preserve the truth
rather than fabricate compatibility.

### The D / H boundary

Stage D owns: persistent per-Agent grants; the call-time permission decision; the tool registry and
the tool execution loop; MCP integration; tool audit; and the minimal operator-owned trust and egress
controls in ADR 0016.

Stage H owns: interactive Ask/Approve/Deny; the encrypted secret manager; sandboxing; resource
limits; path and network isolation; and publisher trust.

Stage D implements **no** interactive approval. `docs/permissions.md` already files
`waiting_approval` under *"Future permission flow"*, and a control that does not exist must not be
rendered as if it did.

## Consequences

The model-facing catalog is a *derived* view over durable grants, so no cache of it may ever be
treated as authority. Every call re-derives its verdict from rows, twice.

The grant table being current-state rather than a ledger is a real limitation, and it is stated
rather than papered over: an operator cannot audit who granted what last week. That is acceptable
because Stage D's audit obligation is about *tool calls*, and every tool call carries the decision
that permitted it.

Not persisting arguments or results makes some debugging harder. That is the deliberate price of not
building a plaintext store of other systems' data before an encrypted one exists.

Rejecting `$ref` makes some legitimate MCP tools unusable until a later milestone. The alternative
was a resolver over untrusted input.
