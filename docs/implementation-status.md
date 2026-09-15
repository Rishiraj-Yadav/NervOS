# NervOS Implementation Status

## Current phase

Stage C — Persistent execution engine is IN PROGRESS. The C0 architecture freeze, the C1 durable execution foundation, C2 — asynchronous submission and minimal durable Worker execution — C3 — Worker registry/health, expired-lease reconciliation, and fencing hardening — and C4 — the safe execution retry engine — are implemented, externally reviewed, and accepted. C5 has not started.

Stage B — Trusted-agent runtime proof is complete and accepted. B1 domain/persistence, B2 internal one-call execution, B3 trusted Agent/Run HTTP API with the minimal Chat dashboard interaction, and B4 second-provider portability are implemented, merged to `main`, and post-merge verified.

## Stage C milestones

- [x] C0 — Durable execution architecture freeze (documentation/governance only; no schema, no implementation)
- [x] C1 — Durable execution foundation (dormant Job/Attempt/RunEvent domain, `0003` migration, schema-parity protection)
- [x] C2 — Asynchronous submission and minimal durable Worker execution
- [x] C3 — Worker registry/health, expired-lease reconciliation, and fencing hardening
- [x] C4 — Safe execution retry engine (durable `SAFE_TO_RETRY` re-execution and backoff)
- [ ] C5 — Not started
- [ ] C6 — Not started
- [ ] C7 — Not started
- [ ] C8 — Not started

C2 passed external source review after a bounded remediation pass, passed every hosted check on pull
request [#11](https://github.com/Rishiraj-Yadav/NervOS/pull/11), and was merged to `main` in merge
commit `21a588452b5a01f5bd099d21ccaacd7f7584b483`. The long-lived `stage-c` branch was
fast-forwarded to that merged state.

C3 passed external source review of its plan, then external implementation review, then a dedicated
E2E acceptance remediation that added the deterministic pre-start recovery journey. C4 passed
external review of its plan, then external implementation review. C4 is authored and verified
together with this status update in a single C4 milestone change, so it deliberately records no pull
request number and no merge commit of its own. C5 through C8 have no implementation and require
separate planning, external plan review, and explicit implementation authorization.

## Stage B milestones

- [x] B0 — Scope freeze and first trusted-agent architecture
- [x] B1 — Agent-instance and run domain/persistence
- [x] B2 — Model adapter, process-secret foundation, and bounded proof runner
- [x] B3 — Trusted Chat Agent API and minimal dashboard interaction
- [x] B4 — Second-provider portability and Stage B final acceptance

B0 is a documentation/governance milestone. ADR 0007 freezes a one-shot trusted `nervos.chat` definition identified by exact key/version, explicit user-owned instances, immutable-snapshot Runs, a narrow application-owned model port, process-only provider credentials, one bounded model call, the `created -> running -> succeeded|failed` lifecycle, and an awaited API-process proof runner. B1 implements the domain/persistence foundation, B2 implements the internal Anthropic execution path, B3 exposes that same path over an authenticated, owner-scoped HTTP API with a minimal trusted Chat UI, and B4 adds OpenAI Responses as a second production adapter behind the unchanged port. ADR 0008 records the B4 portability decision.

## Stage A milestones

- [x] A0 — Stage A plan reviewed
- [x] A1 — Repository and tooling bootstrap
- [x] A2 — FastAPI + typed configuration + SQLite + SQLAlchemy + Alembic
- [x] A3 — First-run setup + local authentication
- [x] A4 — React dashboard foundation
- [x] A5 — Stage A E2E flow
- [x] A6 — CI, tracked-file security scanning, and Stage A documentation completion
- [x] A7 — Final architecture/security/test audit

## Stage A acceptance criteria

- [x] clean checkout can bootstrap dependencies
- [x] API starts successfully
- [x] `/api/v1/health` succeeds
- [x] migrations work from empty database
- [x] first user can initialize NervOS exactly once
- [x] user can log in
- [x] authenticated session survives browser refresh
- [x] logout invalidates server-side session
- [x] protected dashboard is unavailable unauthenticated
- [x] backend tests pass
- [x] frontend tests pass
- [x] E2E smoke flow passes
- [x] Ruff passes
- [x] Pyright passes
- [x] frontend TypeScript check passes
- [x] production frontend build passes
- [x] CI matches local quality commands
- [x] no real secrets are committed

## Not implemented in Stage A

- AgentPackage/AgentInstance execution
- retry/recovery worker engine beyond the minimal C2 durable Worker
- scheduler/event router
- model providers
- MCP/tool gateway
- agent memory
- agent conversation sessions
- package installer
- marketplace
- IoT
- multi-agent orchestration
- user management

## A1 verified checks

- `uv sync --frozen --all-packages`
- `uv run python scripts/check.py check`
- Ruff check and format check
- Pyright strict type checking
- Pytest tooling suite (6 tests)
- ESLint and TypeScript checks
- Vitest foundation (`--passWithNoTests`; product tests begin in A4)
- Workspace membership, ignored-file, syntax, lockfile, and no-product-behavior audits

## A2 verified checks

- `uv sync --frozen --all-packages`
- `uv lock --check`
- `uv run python scripts/check.py lint`
- `uv run python scripts/check.py typecheck`
- `uv run python scripts/check.py test`
- `uv run python scripts/check.py check`
- Pytest backend/tooling suite (51 tests)
- Isolated Alembic upgrade, current/head, no-drift check, downgrade, and re-upgrade
- Migration-first development launcher and exact `GET /api/v1/health` smoke check
- Ruff, Ruff format, Pyright strict, ESLint, TypeScript, and Vitest foundation checks

## A3 verified checks

- `uv sync --frozen --all-packages`
- `uv lock --check`
- `uv run python scripts/check.py lint`
- `uv run python scripts/check.py typecheck`
- `uv run python scripts/check.py test`
- `uv run python scripts/check.py check`
- Pytest backend/tooling suite (100 tests after final handoff remediation)
- Real file-backed setup race exercised 20 times with exactly one user/session
- Manual setup/login/me/logout/replay/login smoke flow over real Uvicorn
- Public setup-status discovery, JSON-only credentials, exact-origin unsafe-API enforcement, received-body limits, and response security-header checks
- Alembic still head `0001_stage_a` with no schema drift
- Dedicated security/test/architecture reviewers: no CRITICAL/HIGH findings; concurrency bound, safe 503 mapping, pre-body Origin/size boundary, setup-complete precheck, and single-owner policy fixes applied

## A4 verified checks

- `pnpm lint`
- `pnpm typecheck`
- `pnpm test` (38 Vitest/Testing Library/MSW tests)
- `pnpm build`
- `uv run pytest tests/test_tooling.py apps/api/tests/integration/test_setup.py apps/api/tests/integration/test_authentication.py apps/api/tests/integration/test_auth_security.py apps/api/tests/integration/test_health.py` (25 passed)
- `uv run python scripts/check.py check` (101 Python tests and 38 frontend tests passed)
- `git diff --check`
- Isolated temporary-database browser smoke: setup, canonical dashboard identity, logout, login, refresh restoration, dashboard, and final logout
- Focused architecture and security reviews completed; wildcard routing and abort-classification findings fixed

## A5 verified checks

- `uv lock --check`
- `pnpm install --frozen-lockfile`
- `pnpm --dir apps/web exec playwright install chromium` (Chromium only)
- `uv run pytest tests/test_e2e.py tests/test_tooling.py` (17 focused supervisor/tooling tests)
- `pnpm --dir apps/web lint`
- `pnpm --dir apps/web typecheck`
- `pnpm --dir apps/web test` (38 Vitest/Testing Library/MSW tests)
- `pnpm --dir apps/web build`
- `uv run python scripts/check.py check` (110 Python tests and 38 frontend tests; E2E remains intentionally separate)
- `uv run python scripts/check.py e2e` twice consecutively, each with a newly Alembic-migrated temporary SQLite database
- Controlled Playwright timeout path, proving supervisor cleanup of only owned process trees
- `git diff --check`

## A6 verified checks

- `uv lock --check` and `uv sync --frozen --all-packages`
- `pnpm install --frozen-lockfile`
- `uv run python scripts/bootstrap.py --skip-browser` (dependency-only path; Chromium deliberately not provisioned)
- `uv run python scripts/bootstrap.py` (A5 default path unchanged; Chromium still provisioned)
- `uv run python scripts/check.py check` (164 Python tests and 38 frontend tests, now including the tracked-file security scan)
- `uv run pytest tests/test_security_scan.py` (36 scanner tests: positive and negative fixtures, placeholder suppression, allow-marker scoping, and a sentinel assertion proving no matched value reaches stdout or stderr)
- `uv run pytest tests/test_clean_check.py` (6 tests, including proof that the export never mutates the active checkout and needs no Git author identity)
- `uv run pytest tests/test_tooling.py` (21 tooling tests, including workflow invariant checks)
- `uv run python scripts/security_scan.py` and `--tracked-only` (176 and 166 files scanned, no findings in either mode)
- `pnpm build`
- `uv run python scripts/check.py e2e` twice consecutively, each with a newly Alembic-migrated temporary SQLite database
- `uv run python scripts/clean_check.py` — all five steps PASS inside an isolated export (lockfile, bootstrap, check, build, e2e) with exit code 0
- Ruff, Ruff format check, Pyright strict, ESLint, and strict TypeScript
- `.github/workflows/ci.yml` and `security.yml` parse as valid YAML, declare `permissions: contents: read`, reference no secrets, and pin every action to a verified full-length commit SHA
- `git diff --check`

### GitHub-hosted validation

Both workflows were then exercised on real GitHub-hosted runners through pull request [#1](https://github.com/Rishiraj-Yadav/NervOS/pull/1) (`stage-a-a6-ci-validation` → `main`), triggered by `pull_request`, at commit `760c4e8`:

- CI — run [34699167313](https://github.com/Rishiraj-Yadav/NervOS/actions/runs/34699167313), attempt 1, `success`
  - `Repository checks` (`check` job): every step succeeded — checkout, pnpm, Node from `.node-version`, uv, `uv python install`, `uv lock --check`, `bootstrap.py --skip-browser`, then `check.py check` — with no Chromium provisioning
  - `Deterministic browser journey` (`e2e` job): every step succeeded — full bootstrap with the project-local Chromium, then the CI-only `playwright install-deps chromium`, then `check.py e2e`
- Security — run [34699167312](https://github.com/Rishiraj-Yadav/NervOS/actions/runs/34699167312), attempt 1, `success`
  - `Tracked-file security scan`: the repository-owned scanner ran on the runner's `actions/setup-python` interpreter and reported no findings

Both runs uploaded zero artifacts and required no retry (attempt 1). The only annotation was a non-failing `astral-sh/setup-uv` cache-reservation warning on the `e2e` job, caused by the two CI jobs starting in parallel and contending for one cache key; the job succeeded and that cache is only an optimization. Workflow job logs are not retrievable without repository credentials, so this record rests on per-step run results rather than raw log text.

## A7 final verification

Pull request [#1](https://github.com/Rishiraj-Yadav/NervOS/pull/1) is merged. Its merged `main` commit is `a76c931e618977d1f6bfe992c22173989d52f4d2`; the merged tree was audited as the final Stage A candidate. The real GitHub-hosted merge-commit validation succeeded on its first attempt: CI run [34699701274](https://github.com/Rishiraj-Yadav/NervOS/actions/runs/34699701274) passed both `Repository checks` and `Deterministic browser journey`, and Security run [34699701202](https://github.com/Rishiraj-Yadav/NervOS/actions/runs/34699701202) passed `Tracked-file security scan`.

A7 remediation added safe persistence-failure HTTP regression coverage for setup, login, current-user resolution, and logout; direct E2E-supervisor regression coverage for default-database rejection and post-run mutation detection; clean-check failure-path coverage for stop-on-first-failure, nonzero status, temporary-export cleanup, unchanged active checkout, and no Git-author requirement; and concise tracked backend/frontend reviewer definitions scoped only to current Stage A review.

Final local verification passed on the uncommitted A7 remediation candidate:

- `uv lock --check`
- architecture tests (4 passed), core unit tests (36 passed), core integration tests (10 passed), API tests (47 passed), tooling/scanner/clean-check/E2E-supervisor tests (75 passed), and the complete Python suite (172 passed)
- ESLint, Pyright strict, frontend TypeScript, Vitest (38 passed), and production frontend build
- local and tracked-only repository-owned security scans (176 files each, no findings)
- isolated Alembic upgrade/current/check/downgrade/re-upgrade with application schema `users` and `auth_sessions`
- `scripts/check.py` lint, typecheck, test, security, and aggregate check gates
- two consecutive deterministic E2E runs, each with a new temporary Alembic-migrated SQLite database
- `scripts/clean_check.py` in a Git-selected isolated export, including its E2E step

No normal NervOS database was created or used. No unresolved blocking finding remains. The A7 documentation, governance, and regression-coverage findings are resolved. No release, tag, deployment, push, or Stage B work occurred.

## Accepted Stage A limitations

- The repository-owned scanner is bounded Git-selected secret hygiene, not a comprehensive dependency audit or SAST; non-SQLite binary and text files larger than 1 MiB do not receive content-rule scanning.
- The Argon2 password-work bound is process-wide rather than host-wide.
- Without an operator bootstrap secret, first-run setup requires a trusted interface; non-loopback deployments need their own network/proxy controls.
- Non-credential endpoints have no global request-body cap.
- Expired or revoked session rows are filtered but not automatically pruned.
- CSRF protection uses exact-Origin enforcement and `SameSite=Lax`, not a separate anti-CSRF token.
- SQLite WAL is deliberately disabled for the local Stage A model.
- Ignored historical Claude worktrees are local housekeeping and are not repository content or acceptance evidence.
- Anonymous GitHub metadata does not expose raw runner logs; run, job, and step metadata supplied hosted validation evidence.

## B0 architecture verification

B0 was implemented as documentation/governance only. ADR 0007 and the Stage B roadmap/runtime documentation define the reviewed B0–B4 boundaries, exact-version trusted definition identity, explicit non-unique user instances, one-shot Runs, three failure classes, immutable execution snapshots, proof limits, process-only provider secrets, and the accepted created/running stranded-state limitation. Backend/frontend reviewer definitions now review only behavior accepted in the current milestone while continuing to flag later-stage scope expansion.

Verification included focused governance tests, both repository security-scan modes, `git diff --check`, the canonical `scripts/check.py check` gate, and the full isolated `scripts/clean_check.py` gate. No source, runtime, migration, route, frontend product UI, provider dependency, secret storage, queue/worker, tool, memory, scheduler, package, SDK, or Marketplace behavior was added.

## B1 implementation verification

B1 implements the exact-version built-in definition resolver, explicit owner-scoped Agent Instances, immutable-snapshot Runs, four-state conditional persistence, the additive `0002_stage_b1_agent_instances_runs` migration, and the narrow provider-neutral model completion contract. It adds no provider, execution coordinator, API, UI, Job, tool, memory, package, or secret behavior.

Verification includes focused domain and persistence tests, migration upgrade/current/check/downgrade/re-upgrade on disposable databases, the canonical repository check, both security scanner modes, two deterministic E2E regressions, and isolated clean-check. `FIRST PROVIDER DECISION REQUIRED BEFORE B2`.

## B2 implementation verification

B2 selects Anthropic as the first provider, activates `packages/nervos-models`, and uses the official async Anthropic Messages API behind the unchanged B1 model port. `ANTHROPIC_API_KEY` is optional process-only secret-aware configuration; `.env` remains unloaded and API startup remains credential/network independent. The trusted `nervos.chat@1` behavior makes one completion/`messages.create` invocation with SDK retries disabled, applies the immutable Run timeout both to the SDK request and an outer async deadline, excludes thinking/redacted-thinking, validates exact bounded text, keeps `total_tokens` NULL, and persists terminal success/failure only after a transaction-free provider await.

Automatic verification uses deterministic fakes and temporary SQLite only. Focused adapter, handler, provider-registry, coordinator, configuration/startup, architecture, B1 persistence, and migration regressions pass, followed by full Python/frontend, scanner, canonical, two-E2E, and clean-check gates. The default NervOS DB remains untouched.

`REAL PROVIDER PROOF NOT EXECUTED — CREDENTIAL/ACCESS UNAVAILABLE`

## B3 implementation verification

B3 exposes the already-proven B2 execution path as authenticated HTTP resources plus a minimal trusted Chat dashboard interaction. It adds no schema change (head remains `0002`), no migration, no new dependency, no second execution path, and no new execution capability.

Implemented: owner-scoped `GET/POST /api/v1/agent-instances`, `GET/PATCH /api/v1/agent-instances/{id}`, `POST/GET /api/v1/agent-instances/{id}/runs`, and `GET /api/v1/runs/{id}`; four thin `AgentService` read pass-throughs; a dedicated Agent/Run error map; an explicit per-route content-type/body-size rule layered onto the existing credentialed-route controls without changing them; a `Cache-Control: no-store` default on versioned API responses; the `/agents` and `/agents/:agentInstanceId` pages; and the `RunItem` persisted-Run card.

A foreign id and a nonexistent id are indistinguishable: the instance `GET`/`PATCH`, the run `GET`, the instance's run-creation `POST`, and the instance's run-list `GET` all return the identical 404 body. The nested run list resolves the owned parent before reading any Run, so an owned instance with no Runs is a `200` empty page rather than a not-found, and a foreign instance's history is a `404` rather than a misleading empty page.

Frozen contract properties: the B3 API permits creation only for the exact `nervos.chat@1` pair; an unknown provider is rejected on creation **and** configuration update while a known-but-unconfigured provider is accepted at configuration time; a `PATCH` is exactly one of two shapes and performs exactly one committed application write; `usage` is null exactly when nothing trustworthy was persisted and `total_tokens` is never derived; a persisted `failed` Run is returned as 201 with the Run body, never as a 5xx; and `POST …/runs` is the only route that creates a Run.

One user submission is one independent Run. The fixed instruction plus the current text is all that is sent; no prior Run's input or output ever becomes model context, and there is no `conversation_id`, `session_id`, or Message record. This is proven by a test asserting that a second Run's observed model request contains only the second input.

Verification: focused application, API, architecture, and frontend tests; migration upgrade/current/check/downgrade/re-upgrade on a disposable database confirming exactly two revisions and no schema drift; the full Python suite; frontend lint/typecheck/test/build; both security-scanner modes; the canonical `scripts/check.py check` gate; two consecutive deterministic E2E journeys, each on its own fresh temporary database; and the isolated `scripts/clean_check.py` gate. The default NervOS database remained absent throughout.

The browser journey runs against a real API subprocess whose supervisor launches a test-only ASGI factory outside the shipped packages and explicitly removes `ANTHROPIC_API_KEY` from the child environment, so automated E2E cannot consume a real credential. Production composition (`nervos_api.main:app`) and `scripts/dev.py` are unchanged.

`REAL PROVIDER PROOF NOT EXECUTED — CREDENTIAL/ACCESS UNAVAILABLE`

## B4 implementation verification

B4 adds a second production provider without changing the core port, the B3 API surface, or the schema. It adds no migration (head remains `0002`), no new REST route (the OpenAPI surface remains exactly the seven B3 operations), and no provider-status endpoint.

Implemented: canonical provider `openai` through the official asynchronous `openai` SDK and the stateless Responses API, alongside the unchanged `anthropic` adapter; one optional process-only `OPENAI_API_KEY` setting with blank-to-unavailable normalization and no `.env` loading; a composition that knows exactly both canonical IDs and constructs each client only when its credential exists, with `max_retries=0` and explicit async close; a static two-option provider selector shared by the Agent create and configuration pages; and provider/model display on each Run card.

Frozen OpenAI contract: exactly one awaited `responses.create` per execution with `store=False`, `background=False`, and `stream=False`, and no conversation, previous-response, tools, metadata, temperature, or reasoning controls. Output is normalized by inspecting typed `response.output` directly: recognized reasoning items are ignored, at most one assistant message is accepted, ordered `output_text` parts are concatenated with no separator or normalization, any refusal dominates and is never exposed, and tool/unknown output items fail closed. Accepted visible text is required only for a completion, so an empty or blank `completed` response is still invalid; a `max_output_tokens` or `content_filter` outcome remains canonical even when the response carries no output item at all, and any partial text accompanying it is discarded rather than exposed. `completed` maps to `stop`; `incomplete:max_output_tokens` to `model_output_incomplete`; refusal and `incomplete:content_filter` to `model_refused`; `incomplete:max_messages`, `incomplete:steered`, unknown or absent incomplete reasons, and `in_progress`/`queued`/`cancelled`/unknown statuses to `model_response_invalid`; `failed` maps conservatively to `model_unavailable`. A non-null structured `response.error` is never serialized. The immutable Run `model_name` snapshot remains the operator's opaque string and is never rewritten by a provider-returned alias.

Usage: OpenAI retains provider-reported input, output, and total counts and never derives them; Anthropic's `total_tokens` remains `NULL` for the same reason. Cache, reasoning, and tool breakdowns are discarded. Typed SDK failures map most-specific-first onto the existing NervOS codes with no string parsing, and external cancellation still propagates.

Pre-acceptance remediation: an earlier adapter revision decided its outcome and then unconditionally demanded visible text, so an empty `incomplete:max_output_tokens` or `incomplete:content_filter` response was misclassified as `model_response_invalid` instead of `model_output_incomplete` or `model_refused`. Normalization order is now structured provider error, then unrecognized status, then any unacceptable output item, then an explicit refusal, and only last the presence of accepted visible text; item validation still applies to every outcome, so a tool or unknown item fails closed even alongside a recognized non-success status. Composition no longer leaves a secret-bearing settings object reachable: each credential is read once locally to build its client, middleware and `app.state` then receive a credential-free copy, credential fields are excluded from repr and serialization, and app-state, log, and OpenAPI reachability are asserted by test. Both production client factories now pin their canonical HTTPS API base URL and additionally own their authentication and scoping headers, so the ambient `OPENAI_BASE_URL`/`ANTHROPIC_BASE_URL`, `OPENAI_CUSTOM_HEADERS`/`ANTHROPIC_CUSTOM_HEADERS`, `ANTHROPIC_AUTH_TOKEN`, and `OPENAI_ORG_ID`/`OPENAI_PROJECT_ID` variables can no longer redirect a canonical provider, replace its authentication, or scope its request. Each factory then replaces the SDK custom-header layer through public `with_options(set_default_headers=...)`, restoring canonical HTTP basics from frozen/public SDK values, the public platform-header family, required protocol headers, and NervOS-owned authentication/scoping policy while applying the public `Omit` sentinel to every ambient-only name. Therefore neither an arbitrary new header nor a hostile value colliding with an SDK-owned canonical name can reach the prepared request. Stage B supports exactly one provider-environment input per provider, its API key. The isolation uses only public SDK mechanisms; the exact boundary is recorded in `docs/runtime.md`.

Verification uses deterministic doubles for both providers only. The E2E supervisor removes both `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` from child environments, installs two distinct provider-identifying offline fakes with fail-closed startup assertions, and runs the same instance with Anthropic, then OpenAI, proving both immutable snapshots survive reload. Cross-provider tests assert one call, disabled SDK retries, the shared canonical finish vocabulary, safe errors, and cancellation propagation for both adapters. A dedicated no-fallback regression proves a failing selected provider makes exactly one call and never invokes the other.

```text
ANTHROPIC LIVE PROOF — NOT EXECUTED
OPENAI LIVE PROOF — NOT EXECUTED
```

## C2 implementation verification

C2 changes the product from awaited API-process execution to durable asynchronous execution. `POST /api/v1/agent-instances/{id}/runs` now returns `202 Accepted` after committing exactly one immutable `Run(status=created)`, one `Job(status=queued)`, and the initial `run.created`/`run.queued` events. The API/control plane validates ownership, exact `nervos.chat@1`, input bounds, and known provider identifiers, but it holds no provider credential, constructs no provider SDK client, composes no handler registry, and cannot claim/start/heartbeat/terminalize Jobs.

A separate `apps/worker` uv workspace member runs the execution plane. The Worker validates the existing Alembic revision, resolves its configured providers once at startup, claims only Jobs for providers it can execute, creates one active Attempt under `BEGIN IMMEDIATE`, commits the execution-start boundary before any provider call, renews the lease while execution and finalization run, invokes the existing trusted Chat path exactly once per Attempt, and atomically terminalizes Attempt + Job + Run with safe Run Events. Claim tokens, prompts, outputs, provider bodies, raw exceptions, and credentials are never logged or written to Run Events.

The dashboard renders a `created` Run as **Queued** and distinguishes the queued and running pending states with truthful copy, then observes the terminal result through polling that runs only while a Run is nonterminal and is bounded, so a stranded Run is never presented as actively progressing. Reloading reads the persisted result rather than browser state.

C2 enforces a hard global pending cap (`NERVOS_MAX_PENDING_JOBS`) in the submission transaction and a node-wide active cap (`NERVOS_MAX_ACTIVE_JOBS`) in the claim transaction, with configurable per-process Worker concurrency (`NERVOS_WORKER_CONCURRENCY`). Claiming is capability-aware and one-winner: a Worker claims only Jobs whose provider it is configured for, and two Workers racing one Job produce exactly one claimant. Each claim rotates a 32-byte claim token and persists one Attempt; the execution-start boundary commits before any provider call; and an independent heartbeat renews the lease across every lease window, including finalization. Every owner write is fenced on ownership **and** an unexpired lease, so lease expiry is authority loss — an expired Worker discards its result, never overwrites, and never requeues. A Worker with no provider credentials starts successfully and claims nothing. A known-provider Run with no capable Worker remains queued rather than being failed. Failed Attempts record `SAFE_TO_RETRY`, `DO_NOT_RETRY`, or `AMBIGUOUS` as evidence, but C2 never writes `retry_wait` and never retries execution. Persistence-finalization retry replays only the fenced terminal database transaction and never re-invokes a model.

Still absent after C2: expired-lease recovery and reconciliation, automatic execution retries, retry scheduling, cancellation, per-Agent concurrency, per-provider concurrency, fairness, queue partitions, a public Run Events API, an event-timeline UI, SSE and WebSockets, a Workers table/registry, worker-health tracking, scheduling, tools/MCP, memory, package installation, marketplace, and persistent secret management.

At C2 completion the migration head was `0003_stage_c1_durable_execution`; no `0004` existed. C2 changed no migration, no ORM model module, and no provider adapter. (C3 later added `0004_stage_c3_worker_registry`; see the C3 section.)

At C2 completion, lease renewal was liveness, not crash recovery. A Job that was already `claimed` or `running` when its Worker died remained stranded — neither retried nor requeued — because C2 shipped no reconciler. C3 supersedes this: expired claims are now reconciled automatically. Legacy `running` Runs with no Job can be closed only by the explicit operator command `python -m nervos_worker --reconcile-legacy-runs`; legacy `created` Runs with no Job are left untouched.

C2 verification added durable submission, capacity, claim-concurrency, terminalization, lease/heartbeat, contention/replay, Worker-loop, Worker-config/credential, multi-Agent, API, frontend polling, E2E-supervisor, and architecture-guard coverage. The accepted candidate passed 642 Python tests, 46 Worker tests, and 91 frontend tests; Ruff lint and format; Pyright with zero errors and zero warnings; frontend lint, typecheck, and production build; the repository security scan (258 files, no findings); the migration upgrade/current/check/downgrade/re-upgrade lifecycle on a disposable database; the deterministic API + Worker + Web browser journey twice; and the isolated `clean-check` gate. The protected migrations, ORM model module, and both provider adapters were byte-identical throughout, and the default `~/.nervos/nervos.db` was unchanged. Every check passed on GitHub-hosted runners for pull request [#11](https://github.com/Rishiraj-Yadav/NervOS/pull/11).

## C4 implementation verification

C4 activates durable execution retry for the one failure class that is positively safe to replay. It adds **no migration**: the C1 schema already reserved `retry_wait`, `available_at`, `retry.scheduled`, and per-Attempt retry dispositions, and C4 writes them from the application and persistence layers. Migration head remains `0004_stage_c3_worker_registry`, and `0001`–`0004` are byte-identical to their accepted C3 state.

**Only one outcome may be replayed.** `model_rate_limited` is the sole normalized failure carrying `SAFE_TO_RETRY`, because it is evidence that the provider declined the request. A timeout, an unavailable transport, an internal failure, and any unrecognized code stay `AMBIGUOUS` and terminal, so an outcome whose remote result cannot be excluded is never blindly replayed. The scheduling transaction re-derives that restriction from the exact code as well as the disposition, so widening the classification map alone cannot widen the replay surface. Provider SDK retries remain `max_retries=0`, adapter code is unchanged, and there is no provider or model fallback.

**Retry is durable state, not a timer.** A safe failure closes the current Attempt as immutable failed evidence (`failed`, `SAFE_TO_RETRY`, the original normalized error, its real start and finish), moves the Job to `retry_wait`, stores the absolute due instant in `available_at`, clears every claim field, and appends `attempt.failed` then `retry.scheduled` — with **no** intermediate `run.failed`. C4 adds **no scheduler daemon, timer row, or held execution slot**: a retry becomes eligible when ordinary Worker polling sees `retry_wait` with `available_at <= now`, and a due retry is claimed through the same `BEGIN IMMEDIATE` path as queued work, with capability filtering, the global active cap, and one-winner semantics intact. `available_at` is an earliest-eligibility boundary, never an appointment. `retry_wait` holds no lease, so it consumes no active capacity; it remains one pending obligation.

**The Run survives the retry.** A Run that has begun executing is still executing while it waits, so it stays `running` with no output, usage, elapsed time, or error, and its original `started_at` is never rewritten. C4 adds no public retry status: read-only Run projections show a still-running Run, and an internal retry is asserted not to leak Job status, attempt number, disposition, deadline, or claim data. Each retry creates a fresh numbered Attempt with a fresh 32-byte token and lease, fenced exactly like a first claim, so a previous incarnation's authority is never revived.

**Backoff is deterministic.** The schedule is 1s, 2s, then 4s capped, with no jitter, no `Retry-After` parsing, and no provider-specific policy. The scheduling anchor is captured **once** per logical finalization and stays fixed across every safe database-only replay, so a replayed transition recomputes the identical deadline and an uncertain COMMIT has exactly one expected value to reconcile against. The transaction's own clock is used only for lease and fencing checks.

**The backoff ordinal is not the claim counter.** It counts this Job's *started* safe execution failures. A Worker that died before the execution-start boundary consumed claim budget but never called a provider, so it does not slow a retry for a request that never happened. `max_attempts` remains a **shared total committed-claim budget**: a pre-start Worker loss consumes a slot a provider retry might otherwise have used. C4 accepts that coupling rather than adding a second hidden counter, because the alternative would either delete execution history or remove the only existing bound on a Worker that crashes deterministically before every start. When the budget is spent, the Job and Run terminalize with the **original** `model_rate_limited` and no `retry.scheduled`; no `retry_exhausted` code was introduced.

**Safety is unchanged where it matters.** The provider is invoked at most once per durably started Attempt, and a database persistence failure never replays it: only proven-uncommitted SQLite BUSY/LOCKED failures are replayed, on a fresh connection, and an uncertain COMMIT is resolved by reading durable state back and adopting the committed result rather than repeating the transition. A stale or expired claim cannot schedule anything — its write affects zero rows and appends no event. A crash **before** a `retry_wait` commit leaves a started Attempt that C3 closes as `execution_outcome_ambiguous` and never replays: NervOS declines a legitimate retry rather than replay a request whose outcome it cannot exclude. A crash **after** the commit leaves a claimless durable retry that needs no recovery and survives a restart, verified by a new Worker incarnation claiming it. C3's own reconciliation still selects only expired `claimed`/`running` claims; expired pre-start `SAFE_TO_RETRY` evidence is never read as an instruction to retry.

**Accepted limitations, recorded rather than hidden.** Run `elapsed_ms` and Run usage remain the **terminal** Attempt's values; they do not include earlier Attempts, retry-wait time, or total Run wall-clock duration, and C4 provides no cumulative cross-Attempt token accounting (per-Attempt accounting would need a schema revision, which C4 does not add). The bounded read-only UI poll (roughly 300 seconds at 2-second intervals) is unchanged and is **not** a completion guarantee: a Run's durable state stays correct across Worker downtime, provider duration, pre-start loss, lease recovery, or host downtime, and a later reload or refetch shows the current state. Richer execution observability remains C7.

**Explicitly not provided by C4.** No cancellation (C5), no fairness, queue partitions, or per-Agent/per-provider concurrency (C6), and no public Run Events API, timeline, Worker dashboard, SSE, or WebSockets (C7). `cancel_requested_at` remains dormant: C4 does not read, write, or prioritize it, and no cancellation endpoint, `cancelled` Run status, or cancel-versus-retry rule exists. C4 does not claim exactly-once execution and claims no provider-side rollback or cancellation.

**Verification.** The accepted C4 candidate passed 71 focused C4 tests; the full Python suite of 704 tests (55 Worker, 158 API); 26 architecture guards; 92 frontend tests; 21 E2E-supervisor tests; Ruff lint and format; Pyright with zero errors and zero warnings; frontend lint, typecheck, and production build; the tracked-file security scan (265 files, no findings); and an `EXPLAIN QUERY PLAN` check proving the widened due-work query still resolves through the existing `jobs` status index rather than scanning. Migration head was confirmed still `0004` with exactly four migrations. A deterministic supervised browser journey proves the retry end to end — a scripted rate limit, a durable `retry_wait`, a still-running Run, a second Attempt after the due instant, and exactly two provider calls for that Run — and asserts from committed state that the retry's own `claimed_at` is not before its `available_at`, that the Run's `started_at` equals the first Attempt's start, and that no `run.failed` occurred. It passed twice consecutively, and `check` and the isolated `clean-check` gate both passed. Protected files were byte-identical throughout and the default `~/.nervos/nervos.db` was unchanged (size 65536, sha256 `f60ed2b32637d314d31a4305c704b5f80ff0db14adbefdff156368cc5f05800f`). No live provider request was made.

## C3 implementation verification

C3 activates durable Worker liveness and recovery. It passed external source review of its plan, external implementation review, and a dedicated E2E acceptance remediation.

**Migration.** `0004_stage_c3_worker_registry` creates the `workers` table and performs one narrow, externally approved `runs` lifecycle evolution. `0001`, `0002`, and `0003` are byte-identical to their C2 state. SQLite cannot alter a CHECK in place, so `runs` is rebuilt value-for-value inside Alembic's `autocommit_block()` — necessary because the migration harness installs `PRAGMA foreign_keys=ON` and wraps migrations in a transaction, which makes an in-transaction foreign-key disable a silent no-op and would fail the drop against `runs`' two RESTRICT children. A `PRAGMA foreign_key_check` afterwards raises if integrity was not preserved. The downgrade refuses rather than rewriting history if any Run was closed before execution started.

**Worker registry.** Each process incarnation registers a durable row and never recycles another incarnation's identity; a restart draws a new id and therefore a new row. `worker_id` is unique, and the row carries only identity and liveness timestamps. The registry heartbeat is **monotonic**: the write refuses to move `last_heartbeat_at` backwards, so a wall-clock adjustment cannot make a healthy process look older. Health is **derived** from timestamps — `stopped` when `stopped_at` is set, otherwise `healthy` or `stale` by heartbeat recency — and is never stored as a mutable state, so it cannot contradict its own evidence. **Stale means "not observed recently", never "definitely dead".**

**Authority.** Worker registry health is observability. The **Job lease is execution authority.** A successful registry heartbeat that finds no live row for this incarnation (or a stopped one) contradicts the invariant that every executing Worker is registered, so the Worker stops accepting new claims and shuts down in an orderly way; in-flight work keeps its own lease. Staleness of a registry row never reclaims a Job.

**Recovery.** Reconciliation selects a Job with `status IN ('claimed','running')` and `lease_expires_at <= now`, re-reads its active Attempt, and CAS-checks the exact claim tuple before mutating. Classification uses only the committed execution-start boundary:

- *Pre-start* (`execution_started_at IS NULL`): the boundary provably never committed. With claim budget remaining, the Attempt becomes `expired` with `SAFE_TO_RETRY` and the Job returns to `queued` with a fixed backoff, so a deterministic crasher cannot be re-claimed in a hot loop; the Run stays `created`. When the claim budget is exhausted, the Job and Run are closed truthfully (below).
- *Post-start* (`execution_started_at IS NOT NULL`): a provider call may have been issued. The Attempt becomes `expired` with `AMBIGUOUS`, and the Job and Run fail with `execution_outcome_ambiguous` using the Run's **real** `started_at` and a **real** `elapsed_ms`. It is **never replayed**.

No provider call is made on any recovery path. Reconciliation runs once at Worker startup, bounded, and periodically thereafter, and needs no configured provider — a credential-free Worker still reconciles. Concurrent reconcilers are one-winner via `BEGIN IMMEDIATE` plus row-count CAS with no leader election.

**The narrow failed-before-start Run shape.** When repeated pre-start Worker loss exhausts a Job's claim budget, the Run is closed as `status='failed'` with `started_at` NULL, `elapsed_ms` NULL, no output, no finish reason, no usage, and `error_code='worker_recovery_exhausted'`. The Run-lifecycle CHECK was relaxed for exactly this case and no other: the started-`failed` branch excludes that code, and the never-started branch requires it. Execution provably never began and no model request was issued for those Attempts, so NervOS records no `started_at` and no `elapsed_ms` rather than fabricating them. **This is not cancellation** — it is an infrastructure recovery-budget exhaustion, and C5 still owns cancellation.

**Explicitly not provided by C3.** No automatic model/provider execution retries, no `retry_wait` scheduling, no execution backoff policy, no cancellation, no per-Agent or per-provider concurrency, no fairness or queue partitions, no public Run Events API, no event timeline, no Worker dashboard, no SSE/WebSockets. Provider SDK retries remain `max_retries=0` and there is no provider or model fallback. C3 does not claim exactly-once execution. C4 later activated the execution retry C3 deliberately left out; the C3 sections below remain the historical record of the state C3 shipped.

**Verification.** The accepted C3 candidate passed 661 Python tests (54 of them Worker tests) and 92 frontend tests; Ruff lint and format; Pyright with zero errors and zero warnings; frontend lint, typecheck, and production build; the repository security scan (261 files, no findings); the architecture guards (24, including new C3 boundary guards); the migration upgrade/current/check/downgrade/re-upgrade lifecycle — including a populated `0003` database — on disposable paths; a dedicated deterministic pre-start recovery browser journey that asserts the durable timeline `attempt.claimed → attempt.expired → recovery.pre_start → attempt.claimed → attempt.started → run.succeeded`, that the abandoned Attempt never crossed the execution-start boundary, and that zero provider calls occurred before recovery; the full deterministic API + Worker + Web journey repeatedly; `make check`; and the isolated `clean-check` gate. Protected files were byte-identical throughout, the default `~/.nervos/nervos.db` was unchanged, and no live provider request was made.

## C0 architecture verification


C0 was an architecture freeze and governance milestone only: it changed no repository file, added no schema, and implemented no behavior. It fixed the durable single-host execution engine boundaries that C1 onward must honor — the separation of Agent Definition, Agent Instance, Run, Job, Attempt, Run Event, and Worker; the Run, Job, and Attempt state vocabularies; the internal `SAFE_TO_RETRY`/`DO_NOT_RETRY`/`AMBIGUOUS` retry disposition; the rule that worker loss before external execution starts is safely recoverable while loss after `execution_started_at` is ambiguous and never blindly replayed; `BEGIN IMMEDIATE` claim serialization with commit before dispatch; the partial unique active-Attempt index; and per-Run `MAX(sequence) + 1` Run Event allocation.

## C1 implementation verification

C1 adds the durable execution foundation without changing product behavior. Migration `0003_stage_c1_durable_execution` creates `jobs`, `job_attempts`, and `run_events`; migration head is now `0003_stage_c1_durable_execution`, and `0001` and `0002` are unchanged.

The durable foundation is dormant. A Job is one internal durable obligation per Run, one Attempt is one claim/execution episode for a Job, and a Run Event is an append-only safe lifecycle fact sequenced within one Run and carrying only narrow typed safe fields. The single partial unique index `uq_job_attempts_one_active` permits at most one active `claimed` or `running` Attempt per Job while leaving multiple historical terminal Attempts legal. The `SAFE_TO_RETRY`, `DO_NOT_RETRY`, and `AMBIGUOUS` retry dispositions are persisted, but no retry engine consumed them at C1 — C4 later activated only the `SAFE_TO_RETRY` path, and `DO_NOT_RETRY` and `AMBIGUOUS` still never retry. `jobs.cancel_requested_at` is the sole dormant future cancellation-request authority, and no cancellation behavior exists.

At C1 completion, before the C2 cutover, the atomic Run + Job + initial-event submission primitive and the per-Run event appender had no production caller. The public `POST /api/v1/agent-instances/{id}/runs` route was unchanged and still executed synchronously, returning HTTP 201 with the terminal Run, so normal Stage B execution created no Job, Attempt, or Run Event row, and no Worker process existed. C2 replaced that route behaviour with durable HTTP 202 acceptance and a separate Worker; see the C2 section above for the current state. No `active_attempt_id` exists in either the schema or the domain.

Schema-parity protection: `alembic check` does not compare SQLite CHECK constraints, so a permanent integration test asserts the migrated schema against the ORM metadata on constraint names, normalized expressions, server defaults, foreign keys, and indexes, and fails if that contract drifts. A pre-acceptance audit found the applied schema was weaker than the ORM declared — 19 CHECK constraints were missing, four expressions differed, and `jobs.max_attempts` lacked its `DEFAULT 3` — and migration `0003` was corrected in place, so the applied schema and the ORM metadata now agree exactly for every table. A negative control confirms the guard fails when a constraint is removed.

Verification: the full Python suite (569 tests), the frontend suite (85 tests), Ruff lint and format, Pyright, the security scan, the migration upgrade/downgrade/re-upgrade lifecycle, the deterministic offline E2E journey, `check`, and `clean-check` all passed. No live provider request was made, and the default database `~/.nervos/nervos.db` was unchanged. C1 passed external implementation and remediation review, was finalized as implementation commit `8e9c9da`, and was merged to `main`.

## Next action

C4 is complete and externally accepted. NervOS durably retries the one execution failure that is positively safe to replay: the current Attempt becomes failed evidence, the Job waits on a committed due instant in retry_wait, and a later compatible Worker creates a fresh fenced Attempt, surviving process restarts with no scheduler. Ambiguous outcomes stay terminal and are never replayed. The control plane still holds no provider credential and cannot claim, start, retry, or terminalize a Job.

The next engineering milestone is **C5 — cancellation and execution-timeout orchestration**. C5 has **not** started, and it requires separate planning, external plan review, and explicit implementation authorization. C5 owns cancellation requests and state, cancellation transitions and timestamps, and the execution-timeout orchestration the roadmap assigns it. `jobs.cancel_requested_at` is still dormant: it has no production writer, C4 does not read or prioritize it, and no `cancelled` Run status or cancellation endpoint exists. Nothing beyond the existing roadmap and the accepted C0–C4 architecture freezes is settled.

Stage C — Persistent execution engine is in progress. The C0 architecture freeze is complete. C1 passed external implementation and remediation review, was finalized as implementation commit `8e9c9da`, and was merged to `main` in merge commit `6d54eac`.

Stage B implementation is complete. B4 passed external implementation review and hosted checks, was finalized as implementation commit `faa52a2`, and was merged to `main` by pull request #8 in merge commit `acb55b3`.

No live provider proof has been executed for either provider; those optional proofs remain separately authorized and non-blocking.

## Maintenance rule

Update this file after every accepted milestone. Do not check an item merely because code was generated; verify its acceptance criteria first.
