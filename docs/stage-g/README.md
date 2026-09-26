# Stage G — Agent Package System (Package Software, Versions, Installation, Lifecycle)

**STATUS: G0 ARCHITECTURE FREEZE — PENDING EXTERNAL REVIEW**

**BASELINE: Pre-Stage-G cleanup commit `c66ab7ddd8e9a1893c90c17989b0d6bb75b45bae`**

This is the canonical Stage-G master plan and the frozen Stage-G architecture authority.
Every Stage-G implementation milestone MUST read this document and accepted ADRs 0024–0026
before changing code. This revision incorporates the external review of the Stage-G0 planning
report (Revision 2) and the G0 documentation freeze. **G0 is architecture and governance only**:
it implements no package code, no migrations, no SDK activation, no resolver, no `.nervos`
builder, no installer, no Worker package-host, no API/UI, and no dependency.

No per-file blob hash is stored here (a self-referential hash would defeat the quickest
tamper check); milestones report it as computed (`git hash-object docs/stage-g/README.md`).

## 1. Verification protocol

At the start of every G1+ turn, a milestone must report:

```text
Stage-G plan blob:        <git hash-object docs/stage-g/README.md>
Accepted Stage-G ADRs:    0024, 0025, 0026
Current milestone:        Gx
Predecessor:              <state>
Baseline SHA:             c66ab7ddd8e9a1893c90c17989b0d6bb75b45bae
Migration head:           0012_stage_f4_conversation_lifecycle (until a G3 migration)
```

A conflict with this plan or an accepted Stage-G ADR is a **stop condition**:

```text
STAGE G ARCHITECTURE CHANGE REQUEST — <issue>
```

Do not silently edit the frozen plan while implementing. External review must accept G0 before G1
begins. Every frozen decision below requires a reviewed architecture change to alter.

## 2. Stage-G purpose

Stage G turns NervOS from a runtime containing built-in agents into a runtime that can
**install exact versioned local agent software**:

```text
developer builds installable agent software
   → something.nervos
   → NervOS safely inspects/verifies it
   → NervOS installs an exact version
   → NervOS exposes its AgentDefinition
   → user creates/configures an AgentInstance
   → an ordinary Run → Job → Attempt → Worker
   → existing Model / Tool / Context / Memory systems
```

Outcome: **agents become installable software.**

Stage G adds a **package/install layer around the existing runtime**. It MUST NOT build another
Run type, queue, Job system, Attempt system, Worker authority, scheduler, ToolLoop authority,
memory system, or Conversation engine. Existing stage authority is unchanged:

```text
Stage C → execution reliability
Stage D → tools and grants
Stage E → triggers / automation
Stage F → conversations / context / memory
Stage G → package software / versions / installation / lifecycle
```

## 3. A–F implementation truth (authority)

The A–F runtime is the accepted implementation. Read `docs/implementation-status.md` for the
verified state, ADRs 0015–0023 for the D–F authority, and `docs/runtime.md`/`docs/database.md`
for current execution and schema truth. The ordinary Run path is the single canonical seam
`insert_run_and_job_on_connection` (`packages/nervos-core/src/nervos_core/infrastructure/database/jobs.py`),
with immutable `RunContextSnapshot` semantics (ADR 0022) and the exact built-in definition/handler
registries (`application/agent_definitions.py`, `application/trusted_chat.py`).

## 4. Stage boundaries G / H / I

- **Stage G** owns package format, manifest validation, config schema, package identity/versioning,
  dependency isolation, integrity verification, signature mechanics, local installation, exact
  execution binding, upgrade, rollback, uninstall. **Stage-G signatures are mechanism, not trust.**
- **Stage H** owns hostile-code sandboxing, process-containment hardening, filesystem/network
  restrictions, resource limits, encrypted secret manager, interactive Ask/Approve/Deny,
  publisher/key trust infrastructure, revocation.
- **Stage I** owns Marketplace: publisher onboarding, hosted distribution, discovery/search,
  ratings/metadata, public listing, publishing/install UX.

G0 leaves clean seams for H and I but pulls none of their architecture into Stage G.

## 5. Core mental model

```text
AgentPackage = distributable `.nervos` artifact
InstalledPackageVersion = one exact node-local installed release
AgentDefinition = runtime-visible exact executable definition derived from that release
AgentInstance = one owner's configured instance of the definition
Runs / Jobs / Attempts / Conversations / Memory / grants / triggers / audit
    = runtime/user state
```

Frozen ownership split:

```text
installed package software/version  = node-global
AgentInstances and user/runtime state = owner-scoped
```

**Install does NOT automatically create an AgentInstance.**

## 6. Package identity (frozen)

Package IDs reuse the repository's existing Agent key grammar exactly
(`packages/nervos-core/src/nervos_core/domain/agents.py`, `AGENT_KEY_PATTERN`):

```text
[a-z][a-z0-9]*(?:\.[a-z0-9]+)+
```

with the repository length bound (≤ 128). Rules: lowercase ASCII only; **validation-only** (no
silent normalization/rewriting); no uppercase, hyphen, or underscore; no Unicode identifier
rewriting; no empty labels. The first label must begin with a letter; **non-first labels may begin
with a digit** (so `com.acme.1x` is **valid**).

Reservation: the `nervos.*` namespace is node/built-in/system-owned. Third-party packages may not
use it.

V1 identity coupling (frozen):

```text
agent_key == package_id
agent_definition_version == package_version
```

Therefore `(package_id, package_version) == (agent_key, agent_definition_version)` for packaged
agents. One release has one definition; **one exact AgentDefinition identity must never resolve to
more than one installed package release**, and installing a second implementation behind an
existing exact definition identity is forbidden.

## 7. Package versions (frozen)

V1 package releases are **strict SemVer 2.0.0**. Examples: `1.0.0`, `1.4.2`, `2.0.0-alpha.1`.
SemVer defines precedence/ordering for upgrade and rollback. PEP 440 remains a Python-dependency
concern only (inside `dependencies/lock.json`) and is never a NervOS package identity.

## 8. Archive format and nested-content rule (frozen)

V1 `.nervos` is a ZIP container:

```text
<package>.nervos
├── manifest.yaml
├── agent.whl
├── config.schema.json
├── README.md
├── assets/
├── dependencies/
│   ├── lock.json
│   └── wheels/
│       └── ...
└── integrity/
    ├── files.json
    └── signature.json
```

Frozen: exactly one `agent.whl`; agent wheel and all dependency wheels are **`py3-none-any`**;
**Python 3.12** for V1; **fully self-contained dependency wheelhouse**; **no network dependency
resolution during V1 installation**; no sdists; no arbitrary build hooks; **no package-owned
SQL/Alembic migrations** (there is no package `migrations/` directory). Native/platform-specific
wheels are outside V1.

A wheel is itself a ZIP, so V1 does **not** forbid all nested archives. Allowed when explicitly
declared and safely inspected: `agent.whl` and `dependencies/wheels/*.whl`. Rejected: undeclared
nested ZIP/TAR/archive payloads, symlinks, hardlinks, special files, traversal entries, absolute
paths, drive/UNC paths.

## 9. Manifest format (frozen)

V1 uses exactly `manifest.yaml` — no alternative JSON manifest. Parsing uses a **strict safe YAML
data subset**: no custom tags, no aliases/anchors, no merge keys; duplicate keys rejected; unknown
fields rejected unless an explicitly defined namespaced extension (e.g. `x-nervos-*`); **parsing
never executes code**. Parsed data is canonicalized to a deterministic internal representation.

Frozen separation: `manifest_version` = the manifest protocol/schema version; `package_version` =
the software release version. They are distinct concepts.

## 10. Manifest responsibilities (frozen)

The V1 manifest may describe: manifest version, package id, package name, package version,
publisher/display metadata, description, AgentDefinition identity, Python/runtime declaration,
entrypoint, NervOS compatibility, configuration schema reference, model capability requirements,
**portable tool requirements**, trigger templates/support, memory behavior metadata, assets,
bounded resource hints.

**These are declarations. The manifest creates no permission authority.** Nothing in a manifest
grants tools, widens memory, or activates triggers.

## 11. Configuration contract (frozen)

Configuration schema is **JSON Schema draft 2020-12 (bounded subset)**. Frozen bounds: schema
≤ 64 KiB; effective config ≤ 64 KiB; nested depth ≤ 32; values are **JSON-compatible values
(including nested objects/arrays) certified against the schema, under the frozen depth/bounds** —
not "JSON primitives only". Server-side validation is mandatory before install and before each Run.
Unknown config properties are rejected by default (deterministic defaults permitted). No
executable configuration migration hooks.

NervOS-specific mutability is modeled by the extension keyword `x-nervos-immutable` on a property.

Upgrade behavior:

```text
old config
  → validate against new schema
  → if valid: continue/rebind
  → if invalid: operator/user must provide valid new configuration before rebind
```

No arbitrary migration script runs.

## 12. Run configuration immutability (frozen)

A Run accepted at time T must execute the effective configuration from T. Future G3 MUST
atomically snapshot or immutably reference the effective configuration at Run submission.
Queued Runs, retries, restart recovery, and delayed execution MUST NOT read live mutable
AgentInstance configuration. The final storage shape (column vs snapshot row/table) is a G3
implementation detail; the **immutable semantics are frozen**.

## 13. Package-aware AgentDefinition resolution (frozen, with G1/G3 split)

G1 introduces **resolver contracts only**:

```text
DefinitionResolver
   ├── BuiltinDefinitionSource
   └── InstalledPackageDefinitionSource (interface)
```

G1 scope: interface, composite resolver semantics, deterministic in-memory/static test source,
exact-version resolution, collision failure, built-in protection. **G1 MUST NOT create package
tables.**

G3 scope: the package-registry migration, SQL-backed installed-package source, restart-safe
persistent resolution. Do not blur the G1/G3 boundary.

## 14. Built-in protection (frozen)

Existing built-ins remain supported. Third-party packages cannot shadow built-in definitions
(package IDs that would collide with `nervos.*` are rejected), cannot replace built-in
definition/handler registries, and cannot cause definition fallback. Resolution semantics:

```text
exact id
or
fail
```

Never "closest", "latest", fallback, or implicit upgrade.

## 15. Worker package execution boundary (frozen — load-bearing)

Package code MUST NOT be imported into the long-lived Worker interpreter. The future execution
shape:

```text
Run → Job → Attempt → Worker
  → PackageExecutionAdapter
  → exact package environment Python
  → package-host subprocess
  → nervos-sdk entrypoint
```

Worker remains the **single Stage-C authority** for claiming jobs, creating Attempts, retries,
cancellation, timeouts, and scheduling disposition. The package host:

- never claims Jobs
- never creates Attempts
- never decides retries
- never controls scheduling
- never owns cancellation
- never accesses the database directly
- never receives raw credentials

The subprocess represents execution of **package policy inside one already-authoritative Attempt**,
not another Worker or execution engine.

## 16. Worker ↔ package-host protocol (frozen responsibilities)

A bounded protocol carries at architecture level: initialization/handshake; immutable Run
configuration; immutable context; package metadata; model request/response; tool
request/response; logging/telemetry; result; normalized error; cancellation/termination behavior.

Worker side remains the authority for: model provider access, credentials, ToolLoop, call-time
grants, tool audit, cancellation, timeout, execution-result mapping, retry disposition. The package
host receives no provider credentials.

## 17. Fail-closed retry semantics (frozen)

A package-host failure must NOT create a new retry class. Existing Stage-C failure-disposition
philosophy governs. Unknown/non-graceful package-host failure remains fail-closed/ambiguous unless
the existing architecture proves it is safely retryable. Stage-G weakens no Stage-C execution
semantics.

## 18. SDK boundary (frozen)

G1 activates `nervos-sdk`. V1 public SDK **may** expose: package entrypoint protocol, request/result
types, `AgentContext`, immutable effective configuration, immutable context, model request types,
tool request types, selected memory/context read representation, package metadata, sanitized
logging/telemetry. It MUST NOT expose: SQLAlchemy `Session`/`Engine`, DB path, ORM models,
infrastructure repositories, Job/Attempt mutation, claim tokens, Worker leases/registry, raw MCP or
provider credentials, secrets, direct trigger persistence, arbitrary Stage-E event publication.
Public SDK types remain deliberately separate from internal infrastructure types.

## 19. Memory boundary (frozen)

Stage F remains authoritative. G adds:

```text
ZERO automatic USER memory writes
ZERO automatic AGENT memory writes
```

Package manifests may declare memory behavior metadata; declarations are **not** authority.
Packages receive only memory/context selected by Stage-F mechanisms. Any package-originated memory
change remains explicit and user-mediated under Stage-F authority. Workspace/shared memory remains
outside Stage G.

## 20. Tool requirement semantics (frozen)

V1 machine-enforced manifest tool requirements cover only **portable built-in tool identities** —
the stable canonical Stage-D built-in identifier (`upstream_name` of a built-in spec in
`packages/nervos-core/src/nervos_core/application/builtin_tools.py`). Never database primary keys,
MCP connection IDs, or node-local tool IDs.

- `required` = must be available/grantable for the AgentInstance to be **READY**
- `optional` = may be absent

Neither grants access; call-time Stage-D permission evaluation remains mandatory.

**Freeze clarification:** informal free-text MCP descriptors are NOT machine-enforced required
tools. Portable MCP requirements are deferred until NervOS has a stable cross-node MCP capability
identity. A package may document external MCP expectations for humans, but they do not participate
in V1 readiness as canonical machine requirements.

## 21. Trigger semantics (frozen)

Packages may declare supported trigger kinds and suggested trigger templates. They are UX
metadata/templates only. Installing a package must NOT create, enable, or activate autonomous
Stage-E triggers automatically. The user explicitly creates/enables automation.

## 22. Dependency isolation (frozen)

Never mutate the shared NervOS Python environment. V1:

```text
self-contained .nervos
  → hashed dependency lock
  → pure-Python wheelhouse
  → content-addressed isolated environment
```

No network dependency resolution in V1. **There is no `NERVOS_PACKAGE_NETWORK` V1 configuration
setting** — there is nothing to configure because V1 installation is offline/self-contained. Such a
setting belongs only to a future reviewed remote-resolution extension.

## 23. Integrity model — no recursion (frozen)

`integrity/files.json` is an exact canonical file manifest (SHA-256 + byte length + canonical
relative path per payload file). `files.json` and `signature.json` **never list or hash
themselves**. The chain:

```text
payload files
  → files.json
  → SHA-256(files.json canonical bytes)
  → content_digest
```

`files.json` entries cover exact payload bytes and canonical relative paths/lengths, including at
minimum: the manifest payload representation (or the exact agreed manifest bytes), the agent wheel,
the config schema, README where packaged/signed, assets, the dependency lock, and the dependency
wheels — but **not** `integrity/files.json` or `integrity/signature.json` themselves.

## 24. Signature — Ed25519 envelope (frozen)

V1 uses exactly **Ed25519**. The signature covers a canonical signed envelope containing:

```text
signature_format_version
package_id
package_version
manifest_version
content_digest
```

Verification-key material: for Stage-G V1, `signature.json` may carry the signer public
verification key (or exact public-key material required for verification) plus its calculated
fingerprint. This means *this signature verifies with this key* — **not** *this key is trusted*.
The operator is shown the fingerprint and explicitly authorizes that package installation. There is
**no persistent publisher trust store in Stage G**. Stage H/I own publisher/key trust, revocation,
and persistent trust relationships. An invalid signature is rejected.

## 25. Optional transport digest (frozen)

If retained, an archive/transport SHA-256 is separate from `content_digest`. It may cover the
final archive bytes for cache identity and transport verification. It does not replace signed
content integrity.

## 26. Reproducibility claim (frozen)

Never rewrite arbitrary payload bytes to force reproducibility. Frozen: payload wheels/assets are
hashed as **exact bytes**; NervOS-generated metadata is canonicalized; archive entry ordering,
timestamps, path separators, archive metadata, and compression parameters are canonical. G2
accepts a **pre-built deterministic `py3-none-any` agent wheel**; G2 does not promise deterministic
wheel construction from arbitrary source. The V1 claim:

```text
same exact package inputs
  → same canonical package output/content digest
```

Machine-local absolute paths never enter the package.

## 27. Archive hardening (frozen target)

V1 inspection must eventually reject: traversal, absolute paths, Windows drive paths, UNC paths,
path collisions, case-insensitive collisions, duplicate archive entries, symlinks, hardlinks,
special files, dangerous device names, trailing Windows dots/spaces, oversized payloads, archive
bombs, undeclared nested archives. Retained review limits:

```text
archive max:      256 MiB
extracted max:    1 GiB
file count max:   20,000
single file max:  256 MiB
nested config depth: 32
```

Safety settings operators may configure can only **tighten** these hard code-level limits, never
raise them.

## 28. Package storage / staging contract (frozen guarantees)

Exact paths are finalized in G3; the guarantees are frozen now:

- package versions are immutable once installed
- safe path derivation; raw package IDs are never trusted as filesystem paths
- staging lives on the same filesystem/volume as the authoritative package store
- activation may rely on same-volume atomic rename
- staging is not independently configured in a way that breaks that guarantee
- Windows-safe component derivation

## 29. Durable installation states (frozen conceptually)

G3 chooses exact tables/schema. Lifecycle semantics frozen now:

```text
INSTALLING → INSTALLED → ACTIVE
failure:    FAILED
removal:    PENDING_REMOVAL → REMOVED
```

A filesystem-only orphan staging directory created before a DB row exists is **not** a durable DB
state; startup reconciliation treats it as debris.

Crash-recovery matrix (frozen):

| # | Crash point | Recovery behavior |
|---|---|---|
| 1 | staging created, no DB row | startup sweep removes age-bounded orphan staging (debris) |
| 2 | DB `INSTALLING`, activation rename absent | verify staged files against digest → complete rename or mark `FAILED`; previous version untouched |
| 3 | files activated, DB `ACTIVE` missing | idempotent activation re-commits `ACTIVE`; if inconsistent, `PENDING_REMOVAL` |
| 4 | environment preparation failure | durable `FAILED`, never `ACTIVE` |
| 5 | activation health-check failure | non-active; previous version untouched |
| 6 | crash during removal | complete `PENDING_REMOVAL` when obligations gone; otherwise stay disabled/`INSTALLED` |

Honesty: a SQLite transaction plus a filesystem rename is **not one ACID transaction**.
Recovery/reconciliation makes the lifecycle convergent.

## 30. Activation health check (frozen)

Before a package becomes `ACTIVE`, after explicit operator trust: package environment complete;
entrypoint exists; SDK/protocol compatibility verified; package-host subprocess starts; a bounded
handshake completes. The health check is the **first permitted execution of trusted package code**;
manifest parsing and package inspection NEVER execute code. Failure leaves previous versions
untouched.

## 31. Upgrade semantics (frozen)

Installing package v2 does not upgrade AgentInstances automatically:

```text
install v2 beside v1
  → validate v2
  → operator explicitly upgrades/rebinds instance
  → validate configuration
  → show capability/tool delta
  → future Runs use v2
```

Queued/running/retryable Runs accepted under v1 remain v1.

## 32. Rollback semantics (frozen)

Rollback means **future instance binding** v2 → v1. It does NOT rewind Runs, reverse tool side
effects, rewrite conversations, restore memory, or undo external actions. V1 has no package-owned DB
migrations and no arbitrary config migration hooks, so no impossible rollback guarantees are
promised.

## 33. Uninstall semantics (frozen)

Uninstall is not deleting a directory. It must account for AgentInstances, queued/running/retryable
Runs, Stage-E triggers, conversations, memory, audit, and snapshots/history. Package code may be
physically removed only when no executable obligations still require it. User/runtime historical
data survives package removal. `PENDING_REMOVAL` is used when appropriate.

## 34. One application-service authority (frozen)

Future package lifecycle operations share one application authority:

```text
CLI ─┐
API ─┼── PackageApplicationService
UI  ─┘
```

No direct DB/package-directory manipulation from UI/CLI. The developer-side G2 builder remains
separate from node installation authority.

## 35. Milestone breakdown

```text
G0 — Package architecture, protocol, versioning, trust, and lifecycle freeze (this document + ADRs 0024–0026; governance only)
G1 — Versioned manifest + public Agent SDK + package-aware definition-resolution contracts
G2 — Deterministic `.nervos` builder + verifier
G3 — Transactional installation + durable registry + package-backed execution
G4 — Lifecycle: upgrade/version coexistence/rollback/uninstall + CLI/API/UI
G5 — Integrated acceptance and Stage-G closeout
```

### G1 scope
Manifest/domain types; strict manifest parser; SemVer/package identity validation; compatibility
validation; config-schema validation; public `nervos-sdk`; resolver/source interfaces; in-memory/
static package-definition source; built-in compatibility. **Explicitly NO** durable package tables,
installer, package execution, or SQL installed-package source.

### G2 scope
Canonical package construction; archive validation; content digest; Ed25519 verification; tamper
detection; dependency-wheelhouse validation (self-contained pure-Python); cross-platform safe
package format. No installation.

### G3 scope
The package-registry migration; durable registry; SQL-backed package-definition source;
installation state machine; package storage; dependency environment (content-addressed isolates);
config storage/snapshot + immutable Run config semantics; Run executable pinning; package-host
subprocess + `PackageExecutionAdapter`; activation health check; reuse of the existing
Run/Job/Attempt/Worker/ToolLoop/Context/Memory engines.

### G4 scope
Side-by-side versions; explicit upgrade/rebind with capability delta; rollback; uninstall; unified
CLI/API/UI lifecycle surfaces over the one `PackageApplicationService`.

### G5 scope
Integrated acceptance and closeout. No major new functionality. Proof packages A and B (§36) plus
full A–F regression.

## 36. First proof packages (frozen)

**Proof Package A — deterministic minimal CI package.** Must eventually prove: manifest,
configuration, SDK, package-host launch, handshake, exact-version binding, restart, upgrade,
rollback, uninstall. Requirements: no network, no live provider, deterministic output, no
privileged tool, real package-execution path.

**Proof Package B — cross-stage integration package.** Must eventually prove: deterministic model
provider double, Stage-D tool grant/invocation, a user-created Stage-E schedule, Stage-F
context/memory consumption, zero automatic memory writes. No special execution path for tests.

## 37. Non-goals

G0–G2 add no package runtime; G3 reuses the existing engines and creates no second execution
system. Stage G does not deliver sandboxing (H), secret manager (H), publisher trust (H/I), or
Marketplace (I). It does not widen memory authority, grant tools, auto-activate triggers, or allow
network dependency resolution in V1. It does not repackage built-ins.

## 38. Cross-platform requirements

V1 is pure-Python (`py3-none-any` wheels) so identical installability is honest on Windows and
Linux. Identity-to-directory derivation never trusts raw package IDs; Windows-safe component
derivation, reserved device names, trailing dots/spaces, and case-insensitive collisions are
handled (see §27–§28). Canonical digests are immune to path separator, line-ending, timestamp,
uid/gid, mode, and compression variance.

## 39. Security invariants

1. Manifest parsing and package inspection never execute package code.
2. Signature validity is mechanism, not trust; per-install operator authorization is required.
3. The package host never receives DB access or raw credentials.
4. No manifest, memory, or package mechanism grants Stage-D tools or bypasses call-time evaluation.
5. No automatic memory writes; no silent trigger activation.
6. The shared NervOS Python environment is never mutated by a package install.
7. The one `insert_run_and_job_on_connection` seam remains the single execution authority; the
   package host is policy inside an Attempt, not an execution engine.

## 40. Stop / change-request protocol

Any conflict with this plan or accepted Stage-G ADR stops implementation and produces:

```text
STAGE G ARCHITECTURE CHANGE REQUEST — <precise issue>
```

No implementation milestone silently modifies the frozen architecture.
