# ADR 0030 — Marketplace Repository Metadata Freshness and Supply-Chain Provenance

Status: Accepted for I0 architecture freeze / pending external acceptance

## Context

Artifact authentication, repository freshness, publisher authorization and provenance solve
different problems. G Ed25519 proves signer/payload integrity, not safe code or complete/current
repository metadata. The [master plan](../stage-i/README.md) records threats and acceptance gates.

## Decision

Stage-G Ed25519 remains mandatory. V1 combines HTTPS, exact version/identity, content/archive
digests, independent local signature verification and explicit local approval. **TUF is deferred
in V1.** No new signing scheme, TUF/Sigstore dependency or repository implementation in I0.

Residual limitation: compromised or stale metadata can hide newer releases, replay old valid
release descriptions, hide revocation/advisories or misrepresent listings/publisher evidence
without forging a package signature. Cached presentation has bounded retention, observed_at,
origin/version and explicit fresh/stale state; it cannot authorize installation/execution or
replace fresh availability checks. No arbitrary fixed cache TTL is frozen; I3 reviews policy.

**Before automatic Marketplace updates, automatic AgentInstance rebind, automatic local
revocation enforcement, metadata-derived execution policy, or any other autonomous
security-critical Marketplace metadata action, a reviewed authenticated repository-metadata
design MUST be adopted first.** Evaluate TUF as the preferred standard. Do not invent a custom
repository-signing protocol. This gate remains necessary even if a future UI already displays
advisories or signed packages. Merely adding metadata signatures without reviewed freshness,
consistency, rollback, root/key and recovery semantics does not satisfy it.

Sigstore/in-toto is optional future provenance/transparency, not replacement artifact signing.
Future CI OIDC trusted publishing can grant short-lived credentials scoped to publisher,
project and reviewed repository/workflow claims. Package private keys remain developer/CI-owned,
never uploaded to Marketplace. Exact implementation requires I2/future extension review.
Verified-publisher language must identify actual account/domain/organization evidence; no safe
package/local trust/sandbox exemption badge follows from it.

## Consequences and alternatives

Manual V1 distribution proceeds with explicit residual-risk disclosure. Automatic metadata
policy is deliberately gated. Replacing G signing with OIDC or Sigstore, calling HTTPS a
rollback defense, and creating bespoke repository signing are rejected. H still owns local
trust, secrets, sandbox and enforcement; I supplies observations only.

## Verification and governance

I3 tests stale/offline presentation; I4 tests fresh exact download and independent verification;
I5 proves warnings without automatic local mutation. Autonomous extensions require prior
authenticated-metadata architecture review and appropriate H/I authorization. This ADR is
governance only, pending external acceptance.
