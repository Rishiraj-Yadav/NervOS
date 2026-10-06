# NervOS Marketplace

## Status and authority

**I0 COMPLETE / EXTERNALLY ACCEPTED.
I1 COMPLETE / EXTERNALLY ACCEPTED. Stage H COMPLETE.**

The canonical contract is [Stage-I master plan](stage-i/README.md), with ADRs
[0027](adr/0027-marketplace-authority-hosted-local-boundary-and-pre-h-distribution-mode.md),
[0028](adr/0028-publisher-identity-project-ownership-signing-key-association-and-immutable-release-lifecycle.md),
[0029](adr/0029-exact-artifact-distribution-local-stage-g-installation-handoff-and-offline-lifecycle.md)
and [0030](adr/0030-marketplace-repository-metadata-freshness-and-supply-chain-provenance.md).
Delivered state remains authoritative in [implementation status](implementation-status.md).

## Delivered I1 foundation

The independent hosted service provides bounded catalog/search, exact release metadata and
verified archive downloads using PostgreSQL 18 and private S3-compatible storage. Production
starts with an empty catalog. Real integration fixtures prove serving behavior; publisher
publication is reserved for I2. See [I1 implementation](marketplace-i1.md) and the
[development guide](marketplace-development.md).

## Target flow

Developers build/sign `.nervos` artifacts locally. Future hosted Marketplace statically verifies,
publishes immutable exact releases and provides bounded discovery/distribution. Local NervOS
downloads exact bytes, independently verifies, obtains explicit artifact authorization and uses
the existing Stage-G installer. An ACTIVE package can then be used for separately explicit
AgentInstance configuration and ordinary Worker execution. The complete publication-to-local-install flow and local Marketplace screens remain future work; existing manual package lifecycle is implemented.

## Boundaries

Hosted publisher identities, PostgreSQL-class metadata and private S3-compatible storage are
separate from local SQLite, opaque dashboard sessions, instance configuration, conversations,
memory, secrets, grants and Runs. One operator-configured origin supports public HTTPS and
explicit admin private HTTPS; loopback HTTP is development-only. No arbitrary user URLs.
Marketplace never executes packages or pushes local runtime mutations. Outage must not affect
installed execution or manual `.nervos` lifecycle. Installation never implicitly creates an
instance, tool grant, trigger or memory fact.

## Pre-H scope and trust

The intentional order is **G → bounded/pre-H I → H hardening**, preserving stage letters.
I1–I5 target publication, safe listings/search, exact distribution, explicit local installation,
update availability and status warnings. Publisher account proof, package signer evidence and
local trust are distinct. Signature/static validity does not establish safe code. Sandbox,
encrypted secret manager, interactive approvals, persistent local trust and local revocation
enforcement remain H responsibilities. No automatic upgrades/rebinds or local revocation action.

Current G packages enforce portable built-in tool requirements; arbitrary portable external
MCP binding requires a separately reviewed future G architecture change. It does not block
core distribution. TUF is deferred for V1, but reviewed authenticated repository metadata is
mandatory before autonomous security-critical metadata actions. See the master plan for the
complete threat model, recovery matrix, future seams and I1–I5 acceptance criteria.
