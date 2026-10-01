# ADR 0028 — Publisher Identity, Project Ownership, Signing-Key Association, and Immutable Release Lifecycle

Status: Accepted for I0 architecture freeze / pending external acceptance

## Context

Hosted account authority must authorize publication without changing G signer semantics or H
local trust. [Stage-I master plan](../stage-i/README.md) defines conceptual entities and recovery.

## Decision

Use hosted MarketplaceAccount/ExternalIdentity (exact issuer/subject), Publisher/Membership,
PackageProject, PublisherSigningKey and ProjectKeyAuthorization. Independent OIDC-capable
identity and hosted sessions require recent authentication for sensitive operations. CLI uses
reviewed native-app-safe authentication and short-lived scoped credentials, without local
session reuse or broad permanent tokens. Persistent credentials use OS storage, no plaintext
fallback. I2 selects provider/protocol/library; I0 fixes requirements only.

One project owns one exact G package ID; `nervos.*` is reserved. A claim does not own a dotted
prefix or establish domain control. Published IDs cannot be recycled. Transfers require both
current and destination ownership authorization, recent auth, race-safe mutation and audit.
Suspension preserves history. Current membership/project/key authority is rechecked at publish.

Only public keys are stored. Active keys may publish with project authorization; retired and
revoked keys cannot authorize new publication. Registration may prove possession without
changing G signing. Rotation adds keys; historical signer attribution never changes. Key
revocation, release revocation and local trust are separate decisions. Private keys stay local/CI.

Reuse G exact identity: package ID equals agent key; exact SemVer equals definition version.
Each published ID/version binds forever to exact archive bytes, archive/content digests,
fingerprint and signed payload. Identical retry may return existing result; any different
evidence is immutable conflict. Unique constraints/conditional objects resolve concurrency.
New code needs a new version. Mutable listing/publisher/assets cannot change executable truth.
Account disablement cannot cascade-delete published releases/artifacts/audit.

UploadOperation follows uploading → uploaded → verifying → verified, with rejected (invalid),
failed (infrastructure) and expired outcomes. PackageRelease follows ready → published.
Distribution is available ↔ yanked → revoked, with direct available → revoked allowed;
revocation is monotonic in V1. Only published releases enter catalog. No upload executes code.
Reuse static G verification and separately check project/key authority; no hosted build,
import, pip-install, package-host/activation check, provider or tool call.

PostgreSQL and objects are not one ACID transaction. Durable operations, quarantine,
content addressing, idempotency and reconciliation handle every crash boundary in the master
matrix. Mutation and hosted audit are atomic within the DB; audit failure rolls mutation back.
Claims/transfers/memberships/keys/publication/status/suspension are audited without credentials.

## Consequences and alternatives

Immutable release history survives ownership and key changes. Replacing same-version bytes,
hosting private keys, treating account proof as safe code, and using local auth for publisher
credentials are rejected. Valid malicious signed code remains possible; H owns containment.

## Verification and governance

I2 proves auth, ownership races, key lifecycle, static rejection, identical/conflicting retries,
recovery and audit atomicity. Conceptual entities are not I0 tables. No auth library or queue
framework is selected. Pending external acceptance; changes require Stage-I review and G review
if package/signature semantics change.
