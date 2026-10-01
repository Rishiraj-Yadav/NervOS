# Hosted Marketplace development — I1

Requirements: Python 3.12+, uv, Docker with its Linux engine and Docker Compose.
The separate service uses PostgreSQL 18; local NervOS retains SQLite.

```text
uv sync --frozen --all-packages
uv run pytest apps/marketplace/tests -m "not marketplace_integration"
uv run python scripts/check.py marketplace-integration
```

The integration command starts digest-pinned PostgreSQL 18.6 and authenticated SeaweedFS 4.48
under a random project name with ephemeral loopback ports, waits within a bound, applies hosted
migrations, provisions synthetic test identities/fixtures, runs real acceptance and tears down its
own containers/volumes. No developer DB or live cloud credentials are used. A stopped Docker
engine is a failure, never a skipped green gate. Images need network access on first pull only.

For a manually operated hosted development environment, provision a separate PostgreSQL database
and private S3 bucket, then inject settings from [the full table](marketplace-i1.md#configuration).
Use fake placeholders such as `postgresql+psycopg://<reader>:<placeholder-password>@localhost:5432/<hosted-db>`
and `https://<private-s3-origin>` in your deployment configuration; do not commit real values or load
a project dotenv file. Development/test may explicitly use a loopback HTTP S3 endpoint.

With a migration identity injected, run `uv run alembic -c apps/marketplace/alembic.ini upgrade head`.
Switch to read-only serving credentials, then run `uv run python scripts/dev.py marketplace`
or `make dev-marketplace` (loopback port 8001). It does not auto-migrate or seed. An empty catalog is
correct until I2 publication exists. No fixture insertion HTTP endpoint or production seed command
is provided. Stop the development server with Ctrl+C; operator-managed dependencies have their own
shutdown lifecycle. The acceptance wrapper always removes its own disposable dependency project.

Final verification commands: `uv lock --check`, `uv run python scripts/check.py check`,
`uv run python scripts/check.py e2e`. Local browser E2E requires no Marketplace/Docker service.
