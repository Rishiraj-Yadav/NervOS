# NervOS Marketplace

I1 activates the independent read-only service `nervos-marketplace-service`, namespace
`nervos_marketplace_service`, backed by PostgreSQL 18 and private S3 objects. It never executes
packages or calls the local runtime. Startup does not migrate or seed; production begins empty.

Install: `uv sync --frozen --all-packages`.
Inject process settings from [the I1 operations guide](../../docs/marketplace-i1.md).
Run `uv run alembic -c apps/marketplace/alembic.ini upgrade head` with a migration identity,
then switch to the SELECT-only serving DSN and run `uv run python scripts/dev.py marketplace`
(loopback port 8001). Local NervOS keeps SQLite and its existing processes.

Acceptance: `uv run python scripts/check.py marketplace-integration`.
Docker's Linux engine is required. The wrapper owns a randomly named Compose project,
ephemeral loopback ports, pinned PostgreSQL 18.6/SeaweedFS 4.48 and cleanup in `finally`.
Credentials in test Compose and `s3-test-identities.json` are public synthetic fixtures.
There is no developer database, cloud credential or mock fallback. Ordinary pytest excludes
integration tests; the explicit acceptance gate runs them without skips.

Publisher workflows, local discovery/UI and installation belong to later milestones.
