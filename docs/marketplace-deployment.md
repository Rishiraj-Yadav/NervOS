# Hosted Marketplace deployment — I1

See [configuration, privileges, schema and read API](marketplace-i1.md).
Run the hosted service separately from local NervOS. PostgreSQL 18, a private S3-compatible
bucket and a dedicated private staging directory are required. No local SQLite fallback,
publisher system, package execution, Marketplace dashboard or Stage-H trust is supplied.

## Start safely

1. Provision the separate database and private bucket.
2. Inject migration credentials and run `uv run alembic -c apps/marketplace/alembic.ini upgrade head`.
3. Replace credentials with the SELECT-only database login and HEAD/GET-only object identity.
4. Inject the remaining `NERVOS_MARKETPLACE_*` process settings from the operations guide.
5. Start `uv run uvicorn nervos_marketplace_service.app:create_app --factory --host 127.0.0.1 --port 8001 --no-access-log`.
6. Put the service behind operator-managed HTTPS and connection/request/bandwidth limits.
7. Probe `/health/live` and `/health/ready`; never use health checks to mutate dependencies.

Startup does not migrate, seed or write storage. Use TLS certificate verification for PostgreSQL
and S3. Keep SDK debug/access/header/query logs disabled. Preserve artifact bytes and headers;
disable compression/transformation and public bucket redirects. Aggregate limits and delivery
timeouts belong to the proxy; application limits are per process.

## Backup and recovery consistency

Back up PostgreSQL catalog evidence and private object bytes together with a documented recovery
point. Retain exact archive digests, manifest evidence and historical distribution state. An object
must not be silently replaced to repair a published row. Restore exact original verified bytes.
If metadata references a missing/wrong object, distribution fails closed with 503. Unreferenced
objects are not exposed. Verify restored byte count and SHA-256 against catalog metadata before
reopening traffic. Readiness proves dependencies/schema, not completeness of every artifact.

Never delete published history or roll back revocation to make a restore appear current. Future
I2 writes require their own reconciliation/audit design. Migration downgrade drops the four tables
and is intended for disposable acceptance databases, not routine production recovery.

Graceful shutdown closes active readers and disposes clients/engine. After a crash, with all service
processes stopped, clean only Marketplace-owned `mp-read-*` files in its dedicated staging directory.
Do not age-delete files that another process may still own. Local installed agents and manual
Stage-G package lifecycle remain independent of hosted outage/recovery.
