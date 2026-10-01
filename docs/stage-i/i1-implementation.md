# I1 delivered implementation details

The frozen [I0 master plan](README.md) remains unchanged and records its freeze-time state.
Current milestone state and verified results belong to [implementation status](../implementation-status.md).

I1 activates `apps/marketplace`, distribution `nervos-marketplace-service`, Python namespace
`nervos_marketplace_service`. Domain and application layers define catalog DTOs, shared Stage-G
validation, sortable SemVer projection and read ports; infrastructure provides independent
PostgreSQL/S3 adapters; thin GET routes compose them through the hosted app factory.

Hosted PostgreSQL support is major 18. Four tables: package_projects, package_listings, artifacts,
package_releases. The independent migration head is `mp0001_catalog_foundation`, version table
`marketplace_alembic_version`. Unique exact release/artifact relationships, restrictive history,
published-evidence triggers, monotonic status evidence, generated simple FTS/GIN and binary
SemVer indexes are described in [the operations guide](../marketplace-i1.md).

GET routes: `/marketplace/v1/packages`, `/{package_id}`, `/{package_id}/versions`,
`/{package_id}/versions/{version}`, `/{package_id}/versions/{version}/artifact` under that
package base, plus `/health/live` and `/health/ready`. There is no publisher or catalog-write API.
Artifact reads use a private bucket and digest-only key; complete file staging verifies SHA-256
and size before delivery, rechecks distribution status, then streams exact original signed bytes.
Yanked delivery requires exact digest acknowledgement; revoked delivery is refused. Transport
checks confer no local signer trust or execution permission.

Real acceptance uses digest-pinned PostgreSQL 18.6 and SeaweedFS 4.48 under disposable Compose;
the separate `marketplace-integration` command/CI job requires real services without mock fallback.
Offline tests remain in ordinary checks. Production-style server smoke tests empty and fixture
catalogs and artifact delivery. Signed fixtures contain an execution sentinel that must never run.

Pre-I2 limits: production begins empty; no publisher account/ownership column, upload, publication
workflow, signing-key registration, local client/UI/install, automatic upgrade/rebind, asset/tag
tables, TUF/Sigstore, portable MCP binding or Stage-H sandbox/trust/secret manager.
Local production modules and migration head/Worker/Scheduler guards remain at `0013`.

See [development](../marketplace-development.md) and [deployment/recovery](../marketplace-deployment.md).
