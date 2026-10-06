# I1 hosted Marketplace operations

Separate hosted read-only FastAPI service under `apps/marketplace`; frozen authority:
[Stage I](stage-i/README.md), ADRs 0027–0030. I2 publication, I3 local discovery/UI,
I4 installation and Stage H isolation/trust were excluded from the I1 acceptance.
The current worktree implements the I2–I4 MVP and integrated I5 journeys; see
[current implementation and acceptance limits](stage-i/i4-i5-implementation.md).
Stage H is now complete; external acceptance is separate.

## Configuration

Process environment only, prefix **NERVOS_MARKETPLACE_**; no dotenv or local runtime settings.
Each suffix below denotes its full prefixed environment variable.

| Suffix | Required/default | Meaning |
| --- | --- | --- |
| ENVIRONMENT | Required | development/test/production |
| DATABASE_DSN | Required, secret | postgresql+psycopg serving DSN |
| LOG_LEVEL | INFO | Service log level |
| S3_ENDPOINT_URL | Required, internal | HTTPS origin; loopback HTTP only in dev/test |
| S3_REGION | Required | Signing region |
| S3_BUCKET | Required, internal | Private bucket |
| S3_ACCESS_KEY_ID | Required, secret | Explicit serving identity |
| S3_SECRET_ACCESS_KEY | Required, secret | Explicit serving credential |
| S3_SESSION_TOKEN | Optional, secret | Temporary token |
| S3_ADDRESSING_STYLE | path | path/virtual |
| ARTIFACT_TEMP_DIRECTORY | Required, internal | Private dedicated staging directory |
| DB_POOL_SIZE | 5, 1–20 | Pool bound |
| DB_MAX_OVERFLOW | 5, 0–20 | Overflow bound |
| DB_CONNECT_TIMEOUT_SECONDS | 5, 1–30 | Connect/pool timeout |
| DB_STATEMENT_TIMEOUT_MS | 3000, 100–30000 | Query timeout |
| DB_TRANSACTION_TIMEOUT_MS | 10000, 1000–60000 | PostgreSQL 18 transaction bound |
| S3_CONNECT_TIMEOUT_SECONDS | 3, 1–10 | Connect bound |
| S3_READ_TIMEOUT_SECONDS | 10, 1–30 | Socket read bound |
| ARTIFACT_OPERATION_TIMEOUT_SECONDS | 120, 1–300 | Complete staging deadline |
| MAX_CONCURRENT_ARTIFACT_READS | 4, 1–16 | Per-process staging/delivery slots |

DSN, credentials, endpoint, bucket and temp path are excluded from settings dumps/repr.
Production requires HTTPS S3 and PostgreSQL sslmode verify-full or verify-ca; prefer verify-full
with hostname and trusted CA. Configuration/startup errors are generic. Inject deployment
credentials; never put them in packages, memory, responses, logs or committed files.

## Deployment and privileges

Provision PostgreSQL **18**, a dedicated database/schema and private bucket. Run
`uv run alembic -c apps/marketplace/alembic.ini upgrade head` with a separate migration identity.
Hosted head: **mp0001_catalog_foundation**, table **marketplace_alembic_version**.
Never use the local NervOS DSN/Alembic tree. Startup does not migrate or seed.

Provision a serving login with database CONNECT, schema USAGE and SELECT on the four catalog
tables and version table. Revoke public schema CREATE and business-table writes. The serving
identity must not own tables, be superuser or inherit writer/DDL privileges. Review future table
grants deliberately. The application also sets default_transaction_read_only=on, UTC, query
and transaction timeouts. Queries materialize DTOs and close DB connections before S3 I/O.

Serving object credentials need bucket readiness/list and exact-object HEAD/GET only
(AWS s3:ListBucket and s3:GetObject on the dedicated prefix). Deny anonymous access and
write/delete/copy/multipart privileges. Admin/fixtures and future publishers use separate identities.
Conditional If-None-Match:* creation is tested using fixture admin; I1 has no production writer.
Configure retention/immutability in deployment; acceptance storage is disposable.

```text
uv run uvicorn nervos_marketplace_service.app:create_app --factory --host 127.0.0.1 --port 8001 --no-access-log
```

For hosted deployment use an HTTPS reverse proxy. Disable artifact compression/transformation,
public-storage redirects, raw URL/header logs; bound request/rate/connection/delivery duration.
Admission is per process. Production starts empty; only disposable tests seed signed fixtures.

## Schema

| Table | Evidence and safeguards |
| --- | --- |
| package_projects | UUID, canonical case-sensitive ID ≤128, UTC creation; unique/bounded/reserved-prefix check; no publisher column |
| package_listings | Project PK/FK; plain text display ≤256 chars/1 KiB, summary ≤4 KiB, description ≤32 KiB; positive revision, UTC update; generated stored weighted simple TSVECTOR and GIN |
| artifacts | Lowercase 64-hex archive digest PK, size 1–256 MiB, UTC creation; immutable evidence, restrictive referenced deletion |
| package_releases | UUID, project FK, exact version ≤64, unique artifact FK, content/signing digests, V1 manifest bytes ≤1 MiB, derived Python/NervOS compatibility, binary precedence key, prerelease, publication/distribution state, revision and UTC timestamps |

Unique(project_id, exact_version) includes build metadata. One archive belongs to at most one
exact release. Restrictive FKs retain history. Migration-owned mp_guard_release/project/artifact
functions protect published evidence/deletion, published project identity and artifact evidence.
ready→published is allowed. Distribution may change available/yanked/revoked with revision +1
and nondecreasing status time; revoked is monotonic. No mutation HTTP endpoint exists.
Status revisions and timestamps cannot move backwards even when distribution is unchanged.

## Public GET API

| Route | Response |
| --- | --- |
| /health/live | Process liveness |
| /health/ready | PostgreSQL 18, exact schema head, authenticated bucket and usable staging |
| /marketplace/v1/packages | Available-release discovery/search cursor page |
| /marketplace/v1/packages/{package_id} | Listing/latest available stable version; published history retained |
| /marketplace/v1/packages/{package_id}/versions | Published versions, unavailable opt-in, prerelease filtering, cursor page |
| /marketplace/v1/packages/{package_id}/versions/{version} | Exact published descriptor including yanked/revoked metadata |
| /marketplace/v1/packages/{package_id}/versions/{version}/artifact | Verified original archive bytes |

q is stripped and bounded to 256 UTF-8 bytes; limit defaults 20, range 1–100. Parameterized
plainto_tsquery(simple) search uses exact-ID priority, floor(ts_rank_cd × 1000000) as BIGINT descending,
then package ID C order ascending. Version order: precedence descending, exact version C ascending
for build ties. No OFFSET, float ordering, Python full-table sort, numeric BIGINT or latest alias.

Strict versioned URL-safe Base64 JSON cursors ≤2 KiB bind route, query/package and filters.
Unknown fields/types/versions and mismatched bindings return 422. Cursors are navigation only;
keyset pagination observes current catalog data, not a snapshot guarantee.

SemVer projection reuses Stage G PackageVersion: three core numbers padded to 64 ASCII digits;
stable flag after prerelease; numeric/text identifier markers, padded numeric digits or ASCII
text, zero terminators. Build is excluded from precedence; exact version breaks ties.
Differential tests compare 176,400 pairs from 420 valid versions (400 generation attempts plus edges),
including 55-digit
numbers and alpha.2 < alpha.10. Real PostgreSQL verifies binary order and latest stable.

## Verified artifact delivery

Key: artifacts/sha256/<first-two>/<archive-sha256>.nervos. No package/user filesystem path.
ETag is never SHA-256. Bounded nonblocking admission returns 429 and Retry-After:5 when full.
Explicit S3 credentials, disabled proxies, bounded timeouts and one total SDK attempt.
HEAD/GET precede private unique-file staging; verify complete length and SHA-256, flush/fsync,
rewind and close the SDK body. A final short DB read rechecks distribution before HTTP delivery.
Corrupt, short/long, missing, unreadable, wrongly declared, timed-out or insufficient-disk objects
return 503 before archive bytes are sent. The staging deadline bounds verification.

Delivery owns the slot until close. Completion, disconnect, status refusal, error and shutdown
close file handles before unlinking on Windows and release admission. Reserve 256 MiB × configured
concurrency; readiness checks one archive, admission checks the full bound. Deployment sets
private directory permissions/ACLs. After a crash remove only mp-read-* files in this dedicated
directory with all service processes stopped; no age-based deletion of live files.

Yanked: require X-NervOS-Acknowledge-Yanked containing exact archive SHA-256, otherwise 409.
Revoked: always 410. Range: 416. No partial downloads, recompression or storage redirect.
Output headers: application/octet-stream, exact Content-Length, attachment filename artifact.nervos,
Cache-Control:no-store,no-transform, X-Content-Type-Options:nosniff, generated X-Request-ID and
**Content-Digest: sha-256=:<base64 of raw 32-byte archive digest>:** per
[RFC 9530](https://www.rfc-editor.org/rfc/rfc9530.html). Transport verification grants no local trust.

## Errors, logging and verification

Envelope: error code, allowlisted message, generated request_id. 422 invalid_request;
404 not_found; 409 release_yanked; 410 release_revoked; 416 range_not_supported;
429 rate_limited; 503 artifact_unavailable/service_unavailable; 500 internal_error.
No dependency exception, DSN or SDK detail is returned. All responses are non-cacheable.
Application logs allow only request ID, route template, status and duration; no query/header/body.

Git finalization exposed a legacy local SQLite Alembic comparison collision: the two G3
dependency UNIQUE constraints share a generated name. The local Alembic comparison hook checks
the full reflected/model UNIQUE signatures on affected tables before removing spurious
name-paired operations. Missing or extra constraints still fail drift checks. This changes no
local schema, migration history, model or runtime behavior; local head remains 0013.

```text
uv lock --check
uv run pytest apps/marketplace/tests -m "not marketplace_integration"
uv run python scripts/check.py marketplace-integration
uv run python scripts/check.py check
uv run python scripts/check.py e2e
```

Dedicated CI job marketplace-integration uses digest-pinned disposable PostgreSQL 18.6 and
SeaweedFS 4.48, no cloud secrets or mock fallback. Ordinary checks include offline tests;
local browser E2E remains independent. Acceptance covers migrations, direct SQL immutability,
FTS/keysets/SemVer, least privilege, anonymous denial, conditional create race, byte equality,
corruption refusal and real server empty/fixture catalog smoke. Architecture guards prohibit
local reverse dependencies and execution imports; signed fixtures contain a never-run sentinel.

Reference sources: [PostgreSQL support](https://www.postgresql.org/support/versioning/),
[Psycopg](https://pypi.org/project/psycopg/), [boto3](https://pypi.org/project/boto3/),
[Alembic](https://pypi.org/project/alembic/),
[SeaweedFS mini](https://github.com/seaweedfs/seaweedfs/wiki/Quick-Start-with-weed-mini),
[conditional operations](https://github.com/seaweedfs/seaweedfs/wiki/S3-Conditional-Operations).
