# ADR 0026 — Package Integrity, Dependency Isolation, and Pre-H Trust Boundary

Status: Accepted for G0 architecture freeze

This ADR freezes the integrity, dependency-isolation, and pre-Stage-H trust model for installable
agent software: a self-contained pure-Python V1 wheelhouse with no network dependency resolution
and no mutation of the shared NervOS environment; the non-recursive `files.json` content digest;
the Ed25519 archive signature whose validity is mechanism, not trust; per-install operator
authorization with no persistent publisher trust store; the reproducibility claim; archive
hardening limits; and the explicit non-sandbox guarantee that assets the Stage-H boundary. Canonical
authority is `docs/stage-g/README.md`.

## Context

An installable package that can pull arbitrary dependencies at runtime, or whose signature is
misread as trust, would undermine the self-hosted single-node guarantees Stages A–F established.
NervOS does not yet sandbox hostile code (Stage H). This ADR therefore freezes what Stage G may
actually claim — verified bytes, verified signatures, isolated dependency environments, and
operator-mediated trust — and what it must explicitly not claim.

## Decision

### Dependency isolation

V1 is a fully **self-contained `.nervos`**: a hashed dependency `lock.json` and a pure-Python
(`py3-none-any`) wheelhouse shipped inside the archive. Installation builds a **content-addressed
isolated environment** and **never mutates the shared NervOS Python environment**. There is **no
network dependency resolution in V1** and therefore **no `NERVOS_PACKAGE_NETWORK` V1 setting**;
remote resolution is a future reviewed extension. V1 requires Python 3.12 and rejects sdists,
build hooks, and native/platform-specific wheels.

### Integrity (content digest)

`integrity/files.json` is a canonical file manifest (SHA-256 + byte length + canonical relative path
per payload file) over the packaged payload — at minimum the manifest payload representation (or the
agreed exact manifest bytes), the agent wheel, config schema, README where signed, assets, the
dependency lock, and dependency wheels. **`files.json` and `signature.json` never list or hash
themselves** (no self-reference). The content digest:

```text
payload files → files.json → SHA-256(files.json canonical bytes) → content_digest
```

### Signature (Ed25519)

V1 verifies with exactly **Ed25519** over a canonical signed envelope containing
`signature_format_version`, `package_id`, `package_version`, `manifest_version`, and
`content_digest`. `signature.json` may carry the signer public verification key (or exact
verification material) plus its calculated fingerprint. This establishes only "this signature
verifies with this key" — **not** "this key is trusted". The operator is shown the fingerprint and
explicitly authorizes each package installation. Stage G has **no persistent publisher trust
store**; publisher/key trust, revocation, and persistent trust relationships belong to Stage H/I.
An invalid signature is rejected.

### Transport digest (optional)

An archive/transport SHA-256 of the finished archive bytes may be retained for cache identity and
transport verification; it is distinct from `content_digest` and never replaces signed content
integrity.

### Reproducibility

Payload wheels/assets are hashed as **exact bytes**; NervOS-generated metadata is canonicalized;
archive entry ordering, timestamps, path separators, archive metadata, and compression parameters
are canonical. G2 accepts a pre-built deterministic `py3-none-any` agent wheel and does not promise
deterministic wheel construction from arbitrary source. The V1 claim is narrow: same exact package
inputs → same canonical package output/content digest. Machine-local absolute paths never enter the
package.

### Archive hardening

V1 inspection must eventually reject traversal, absolute paths, Windows drive/UNC paths, path and
case-insensitive collisions, duplicate entries, symlinks, hardlinks, special files, dangerous
device names, trailing Windows dots/spaces, oversized payloads, archive bombs, and undeclared
nested archives (declared `agent.whl`/`dependencies/wheels/*.whl` are allowed). Retained limits:
archive ≤ 256 MiB, extracted ≤ 1 GiB, file count ≤ 20,000, single file ≤ 256 MiB, nested config
depth ≤ 32. Operator settings may only tighten these hard code-level limits, never raise them.

### Pre-H trust boundary (explicit non-sandbox guarantee)

Stage-G environment isolation and the subprocess/SDK boundary are **dependency and architectural
isolation, not hostile-code containment**. Until Stage H provides real sandboxing, resource limits,
filesystem/network restrictions, and process hardening, a hostile package running as the same OS
user cannot be prevented from reading files or the network by the SDK boundary alone. Stage G
installs **explicitly operator-trusted local packages**; signatures verify integrity and signing
key, and the operator decides trust. Stage H owns containment, the encrypted secret manager,
interactive approval, and persistent publisher trust.

## Consequences

V1 deliveries are reproducible, tamper-evident, and installable offline into isolated environments
without touching the shared runtime, giving a credible supply-chain posture for self-hosting. The
cost is intentional: only pure-Python self-contained packages are supported in V1, and no human
should mistake the SDK boundary for a sandbox — a documented, binding limitation that keeps the
Stage-H seam explicit rather than silently overclaiming security.