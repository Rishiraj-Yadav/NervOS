# ADR 0036 — Publisher Trust, Revocation, and Local Enforcement

Status: Accepted for Stage H implementation (H5)

## Context

Stage G signatures are **mechanism, not trust** (ADR 0026): a valid Ed25519 signature proves
bytes, not intent, and installation required per-install operator authorization with no
persistent trust store. Stage I publishes with hosted keys and distribution states
(`available → yanked → revoked`) but explicitly cannot mutate local trust or enforce revocation
(ADR 0027/0030: *"H may define enforcement"*). Stage H now defines it — locally only, without
hosted write authority.

## Decision

### Trust records

A local, owner-visible **publisher trust store** keyed by signer fingerprint (the same
fingerprint format Stage G verification already emits):

```text
state ∈ { trusted, untrusted, revoked }
decided_by, decided_at, reason (bounded, operator-authored), source ∈ { manual, marketplace }
```

- `trusted` allows one-click install of *new* versions from the same signer; **every install
  still shows exact evidence** (ID, version, content/archive digest, signer).
- `untrusted` is the default for unknown signers and behaves exactly like today's per-install
  explicit authorization, with a visible warning.
- `revoked` is terminal for trust: refuse new installs, refuse rebind/rollback **to a revoked
  signer's version**, and refuse **new** execution admission for instances bound to revoked
  versions.

### Revocation semantics (the part that must not lie)

- **Already-installed packages stay installed.** Removing software or rewriting history is not
  revocation. Runs already accepted keep their immutable snapshots (ADR 0025); **queued and
  in-flight Runs continue under their accepted executable identity** — admission-time revocation
  is enforced at *new* claim boundaries for the next safe pre-dispatch check, consistent with
  Stage D's revocation timing.
- New execution admission for a bound-but-revoked version fails closed with a static, safe
  error; the instance is shown as blocked in the UI with the reason.
- Historical Runs are never rewritten; their evidence records which signer they ran under,
  and that record remains true after revocation.
- Un-revoking is a *new explicit trust decision* (`trusted` via the same review flow), never an
  implicit rollback of the revocation.

### Digest and version verification

Connect trust to the Stage-G installer exactly where it already verifies: install authorization
records signer fingerprint + content/archive digests (Stage I4's `marketplace_install_requests`
already carries them); the trust check composes with `PackageApplicationService.install`
(unchanged) as an **additional gate before installation begins**, not a second installer.
Version selection remains exact — trust never substitutes "latest".

### Manual packages and provenance

Manually installed packages carry their signer fingerprint through the same store. A manual
install of a revoked signer is refused. No inference of Marketplace origin for pre-existing
manual installs (ADR 0030 §48).

### What Stage H does not add

- No hosted mutation authority: revocation here is a *local* operator/owner decision informed
  by Marketplace observations (an advisory or hosted `revoked` status may prompt it, and the UI
  may show the observation, but hosted state cannot flip local trust by itself).
- No automatic updates, automatic rebinds, or metadata-derived execution policy (ADR 0030 §44's
  TUF gate stands).
- No new execution engine; the check is one predicate inside existing admission/install flows.

## Consequences

- A revoked signer's packages cannot be newly installed, rebound, or newly executed locally,
  while durable history and running work remain truthful — the same "revocation at the next safe
  boundary" honesty as every other NervOS security action.
- The operator gains a real trust surface over installed software without turning the local
  runtime into a Marketplace agent.
