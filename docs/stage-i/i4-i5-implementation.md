# Stage I4/I5 Marketplace MVP implementation and verification

Date: 2026-10-02. This report describes the current worktree. It does not grant
external acceptance or change the frozen Stage-I master plan.

## Architecture and implementation

The plan is `i4-i5-plan.md`. Hosted Marketplace persistence remains separate from
local NervOS. Publisher mutations use a separately configured writer engine;
catalog reads continue through the read-only engine. Quarantine and immutable
finalization use separate S3 identities. Static verification invokes the existing
Stage-G parser in a credential-free child with a 2 GiB memory ceiling, a 60-second
CPU budget and a 120-second parent deadline. It neither imports nor runs agent code.
The process limits are not a Stage-H hostile-code sandbox.

Hosted routes now expose key proof/project authorization, bounded archive upload,
leased verification, explicit publication, listing and distribution changes.
Publication is a separate action after verification. Exact finalized S3 version
references are preserved. Browser mutations require CSRF; publisher CLI credentials
are independent of local runtime sessions. The publisher CLI uses browser PKCE,
explicit consent and a supported OS credential backend without plaintext fallback.
Logout revokes the hosted credential before removing its local entry.

Local migration `0014_stage_i4_marketplace_install_requests` adds only install
tickets. Its ORM metadata and Worker/Scheduler head guards agree. It changes no
Run/Job/Attempt, package registry or executable-resolution authority. Existing
manual installs remain on the same Stage-G installer.

Tickets bind authenticated owner, configured origin, exact package/version, size,
archive/content digests, signer and distribution revision/state. Preparation
downloads and independently verifies the retained archive. Approval is separate
and hands the same archive and exact authorization to `PackageApplicationService`.
This is the sole installer and repeats snapshot verification before activation.
Installation does not create an AgentInstance or grant tools.

Downloads are bounded to 256 MiB, 120 seconds total, and configured per-read timeout.
Catalog JSON is capped at 1 MiB. Redirects and inherited proxies are refused.
Actual socket addresses are resolved, classified and pinned per connection; TLS
uses the original hostname. Private HTTPS requires `NERVOS_MARKETPLACE_ALLOW_PRIVATE=true`;
loopback HTTP requires development/test mode. The small HTTPX/httpcore backend
adaptation is isolated in `marketplace_transport.py` and covered by regressions.

Tickets expire after one hour. At most 16 active tickets and 512 MiB of reserved
artifact bytes are admitted node-wide. Cancellation and successful installation
remove retained bytes. New request creation expires idle tickets and cleans old
unreferenced archives. A crash after Stage G commits ACTIVE can be reconciled by
retrying the same approval: all three installed digests must match, and installation
is not dispatched again. An approved ticket without matching ACTIVE evidence is
refused; it does not guess whether an interrupted G operation succeeded.

There is no atomic transaction across hosted status and local installation. Status
is freshly checked before handoff; a changed revision/state/digest fails the ticket.
Changes occurring after that observation remain a documented remote race. Remote
yank/revoke never mutates installed software or historical execution policy.

## Verified journeys and checks

- Real PostgreSQL 18 and authenticated SeaweedFS: **52 passed**, no skipped
  integration acceptance. The command was `uv run python scripts/marketplace_integration.py`.
- The combined real hosted/local journey proves sign/key proof/upload/static
  verification/explicit publication, local discovery/exact download/local
  verification/explicit approval/ACTIVE installation, explicit AgentInstance,
  real Worker/PackageExecutionAdapter/package-host/AgentResult execution.
- v2 installs side-by-side while the instance stays on v1. Explicit rebind produces
  a v2 result and leaves the v1 Run JSON unchanged.
- Yank/revoke observations are visible through the local API; both installed
  versions stay ACTIVE and the revoked installed version still executes.
- After the actual hosted HTTP server stops, discovery returns 503 and the installed
  agent still runs successfully. No live AI provider is used.
- PostgreSQL credential-broker acceptance proves browser binding, CSRF, explicit
  consent, PKCE, one-use authorization code, and revoked CLI token rejection with a
  deterministic identity port. This is not live external OIDC-provider qualification.
- Local/CLI/transport/architecture focus: **30 passed**, including substitution,
  cancellation, expiry, owner isolation, completed-install reconciliation, OS-only
  credential storage and connection-time DNS refusal.
- Marketplace UI behavior: **2 Vitest tests passed**; listings render as text,
  verification does not auto-approve, and cancellation calls the server.
- Migration/architecture focus: **120 passed** earlier in this implementation run.
- Extended supervised browser acceptance: **4 Playwright journeys passed**, exit 0,
  including Marketplace explicit review/approval and revoked-release refusal using
  deterministic presentation responses, plus the existing A/E/G real journeys.
- `uv lock --check` and `pnpm build` passed. Ruff check/format, frontend lint and
  Python/frontend type checks passed in the final full-check invocation.
- Final `uv run python scripts/check.py check`: **exit 0**. **3,271 Python tests
  passed**, 52 opt-in hosted tests deselected here and passed separately; **154
  frontend tests across 16 files passed**. Ruff check/format, frontend lint,
  Python/frontend type checks, and security scanning passed (700 files, no findings).
  Python reported 10,529 warnings, primarily existing deprecations; no failures were hidden.

The working I2–I4 MVP and integrated I5 demo acceptance are verified. Formal
Stage-I production/external acceptance remains pending the requirements below.

The initial full-suite run reported 3,250 passes and three stale schema/publication
guard expectations. Those expectations were corrected to enumerate the authorized
I2/I4 boundaries, preserving serving read-only and hosted/local isolation checks.
Two later full runs were stopped after identifying respectively the missing
`marketplace_allow_private` expected configuration field and a false-positive `sse`
substring match in the transport variable `addresses`. The configuration suite now
passes all 40 tests; the architecture suite passes all 87 tests with the protocol
check narrowed to an actual SSE/library name. No execution-surface guard was removed.
The final complete check was restarted after these corrections.
The initial new browser test used the wrong login button label; its selector was
corrected from “Log in” to the existing “Sign in”; its post-login assertion now uses
the actual dashboard heading, since the dashboard route is `/`. Generated Playwright bundles are
excluded from application lint; source and tests remain included.

## Scope and remaining acceptance

This implements the requested working Marketplace MVP. I0/I1 retain their existing
external acceptance; no external acceptance of I2–I5 is implied by this report.
Production closeout still requires independent review, live configured OIDC/TLS
qualification, supported Unix retained-ticket/runtime qualification, the complete
publisher administration/transfer/moderation and publication fault-injection matrix.
MVP publisher onboarding is self-service ACTIVE, as recorded in the earlier MVP
report; that does not claim the frozen production moderation workflow is delivered.
The browser presentation journey uses deterministic catalog responses; the real
hosted publication/storage/install/runtime journey is a separate integration test.

Frozen plan blobs remain F `62cfc0a8039e233c82e2a30ff3fd59495e99395d`,
G `4554ab5e93b35f4bbc4daed164ee5712077c1006`,
I `7bf5cff40eb7ed5defe2d7598b72e0606d29ce7b`.
No developer runtime database was migrated. Git finalization is tracked in PR #44.
Its first hosted check exposed Linux Pyright evaluating the Windows-only WinDLL
function. An explicit Windows platform guard resolves that portability issue;
targeted Linux and Windows type checks and Ruff checks pass. Resource-limit
values and the platform enforcement paths are unchanged. Hosted CI is rerun for
the resulting commit before merge.
The next Linux run passed type checks but found three differential cases caused
by Python 3.12.15 changing compat32's leading-fold whitespace normalization from
the local Python 3.12.5 behavior. A constant-size policy probe now selects the
installed reference rule without feeding large metadata into the full parser.
The existing differential corpus remains intact; the full hosted check is rerun.

## Demo workflow

1. Configure and start the hosted Marketplace with its own PostgreSQL/S3 and OIDC
   settings. `uv run python scripts/dev.py marketplace` disables raw access logs.
   Use the hosted deployment guide for role/credential separation and HTTPS.
2. In the local API process, configure `NERVOS_MARKETPLACE_ORIGIN` to that exact
   HTTPS origin. For intentional private HTTPS also set
   `NERVOS_MARKETPLACE_ALLOW_PRIVATE=true`. Local HTTP is for development/test only.
3. With local services stopped and a normal operator backup, upgrade the local
   schema using the documented Alembic command to head 0014. Prepare the existing
   Stage-G runtime wheels through bootstrap, then start API, Web and Worker.
4. Dashboard → Marketplace → choose an exact version → Review/Download and verify.
   Inspect the signer/digests and trust warning, then separately Approve and install.
5. Open Packages → create/configure an AgentInstance → submit a Run with Worker
   running. Installation alone does not create an instance.
6. For updates, install the new exact version and explicitly rebind the instance
   through the existing package lifecycle UI. Historical Runs remain on their version.
7. Stop Marketplace and submit another Run: installed execution remains local.

Publisher CLI examples use the module so they work before entrypoint synchronization:

```powershell
uv run python -m nervos_cli.publisher --origin https://marketplace.example login
uv run python -m nervos_cli.publisher --origin https://marketplace.example create-publisher example "Example publisher"
uv run python -m nervos_cli.publisher --origin https://marketplace.example claim-project <publisher-id> com.example.agent
uv run python -m nervos_cli.publisher --origin https://marketplace.example logout
```

`upload <project-id> <archive.nervos> --ownership-revision <n> --idempotency-key <key>`
uploads and statically verifies but does not publish. Prove and authorize the public
signing key through the hosted API before upload; keep private signing material on
the publisher machine. The CLI `request <path> <payload.json>` exposes these hosted
JSON actions, including explicit publication; it refuses auth paths. Never supply a
private key or local runtime provider secret to Marketplace.
