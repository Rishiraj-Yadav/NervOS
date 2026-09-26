# ADR 0024 — Package Identity, Manifest, Compatibility, and Configuration Protocol

Status: Accepted for G0 architecture freeze

This ADR freezes the package-identity and manifest-contract protocol for installable agent
software: the exact `package_id` grammar, the SemVer release grammar, the package/definition
identity equality, the `.nervos` archive layout, the strict `manifest.yaml` contract, the
configuration-schema contract with immutable Run-configuration semantics, and the rule that
manifest declarations (tools, memory, triggers) are requests, never authority. Canonical authority
is `docs/stage-g/README.md`; Milestone G1 implements this protocol's contracts.

## Context

NervOS today knows agents only because they are compiled into the repository (`nervos.chat@1/@2`
exact definitions in `application/agent_definitions.py`). Stage G must let a developer's
versioned `.nervos` artifact become an exact, restorable AgentDefinition without weakening the
existing exact-version, no-fallback discipline. Any identity that allows silent normalization,
version substitution, or shadowing would violate settled Stage-B/F rules, so G0 freezes the
protocol before any package machinery exists.

## Decision

### Package identity

- `package_id` reuses the repository's exact Agent key grammar verbatim (the `AGENT_KEY_PATTERN`
  in `packages/nervos-core/src/nervos_core/domain/agents.py`): `[a-z][a-z0-9]*(?:\.[a-z0-9]+)+`,
  with the repository length bound (≤ 128).
- Rules: lowercase ASCII only; **validation-only** (no silent normalization or Unicode rewriting);
  no uppercase, hyphen, or underscore; no empty labels; the first label must begin with a letter.
  Non-first labels may begin with a digit (e.g. `com.acme.1x` is **valid**).
- The `nervos.*` namespace is reserved for node/built-in/system software; third-party packages may
  not use it.

### Package versions

- V1 package releases are **strict SemVer 2.0.0** (`MAJOR.MINOR.PATCH[-prerelease][+build]`), which
  defines precedence for upgrade and rollback. PEP 440 remains a Python-dependency concern only
  (inside `dependencies/lock.json`), never a NervOS package identity.

### Definition identity coupling

V1 couples package and definition identity by construction:

```text
agent_key == package_id
agent_definition_version == package_version
```

so `(package_id, package_version) == (agent_key, agent_definition_version)` for packaged agents.
One release has one definition; one exact AgentDefinition identity must never resolve to more than
one installed package release, and installing a second implementation behind an existing exact
definition identity is forbidden. `manifest_version` (the protocol/schema version of the manifest)
is a distinct concept from `package_version`.

### Archive layout

V1 `.nervos` is a ZIP: `manifest.yaml`, exactly one `agent.whl` (`py3-none-any`), a
`config.schema.json`, `README.md`, `assets/`, `dependencies/` (`lock.json` + `wheels/`), and
`integrity/` (`files.json` + `signature.json`). V1 requires Python 3.12, a fully self-contained
pure-Python wheelhouse, and **no network dependency resolution**, no sdists, no build hooks, and
no package-owned SQL/Alembic migrations. Because a wheel is itself a ZIP, nested archives are
allowed only for the declared `agent.whl` and dependency wheels, safely inspected; all other
nested archives, symlinks, hardlinks, special files, traversal entries, absolute paths, and
drive/UNC paths are rejected.

### Manifest contract

The manifest is exactly `manifest.yaml`, parsed as data only with a strict safe YAML subset (no
custom tags, aliases, anchors, or merge keys; duplicate keys and unknown fields rejected; parsing
never executes code) and canonicalized deterministically. It may describe package id/name/version,
publisher/display metadata, description, AgentDefinition identity, Python/runtime declaration,
entrypoint, NervOS compatibility, configuration schema reference, model capability requirements,
portable tool requirements, trigger templates/support, memory-behavior metadata, assets, and
bounded resource hints. **None of these declarations create permission authority.**

### Configuration contract

Configuration schema is JSON Schema draft 2020-12 (bounded subset). Frozen bounds: schema
≤ 64 KiB; effective config ≤ 64 KiB; nested depth ≤ 32; values are JSON-compatible values
(including nested objects and arrays) certified against the schema — not restricted to primitives.
Server-side validation is mandatory before install and before each Run; unknown configuration
properties are rejected by default; deterministic defaults are permitted; there are **no
executable configuration migration hooks**. Mutability of a property is modeled by the NervOS
extension keyword `x-nervos-immutable`, never a bare JSON-Schema keyword.

### Immutable Run configuration

A Run accepted at time T must execute the effective configuration from T. A future G3 MUST
atomically snapshot or immutably reference the effective configuration at Run submission, so that
queued Runs, retries, restart recovery, and delayed execution never read live mutable AgentInstance
configuration. The storage shape (column vs snapshot row/table) is a G3 implementation detail; the
immutable semantics are frozen.

### Tool, memory, and trigger declarations

Machine-enforced manifest tool requirements cover only portable built-in tool identities (the
stable canonical Stage-D built-in identifier), never DB primary keys, MCP connection IDs, or
node-local IDs; `required` affects readiness only and never grants access; call-time Stage-D
evaluation remains mandatory. Manifest memory behavior metadata and trigger templates are
declarations with no authority: zero automatic USER/AGENT memory writes, and package installation
never creates/enables/activates Stage-E triggers automatically.

## Consequences

The protocol gives determinism and auditability before mechanism exists: identities are
canonical and collision-safe, releases are unambiguous, and no package can widen Stage-D/E/F
authority at install time. V1 accepts the cost of a strict manifest format and self-contained
pure-Python packages, deferring remote dependency resolution, native wheels, and portable MCP
requirements to future reviewed extensions. `docs/stage-g/README.md` and this ADR constrain every
G1–G5 implementation.