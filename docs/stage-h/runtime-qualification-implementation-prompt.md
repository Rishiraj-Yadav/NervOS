# NervOS runtime qualification and Stage-H closeout implementation prompt

Use the following prompt from the repository root:

---

You are implementing the approved NervOS runtime-qualification and Stage-H closeout plan in
`R:\7_sem\Major-Project\Nervos-self-hosting`.

Work autonomously in the approved scope, in the order below. Do not stop after a partial fix and do
not declare completion from focused tests alone. Preserve all user changes in the dirty worktree.
Do not commit, push, open a pull request, merge or modify external production systems unless the
user separately requests Git finalization or deployment.

## Objective

Deliver a reproducibly verified NervOS runtime that:

1. preserves the existing Run → Job → Attempt → Worker execution authority;
2. executes untrusted `.nervos` package code only through a pre-exec sandbox that proves filesystem
   and direct-network denial on an explicitly qualified platform;
3. refuses package installation health checks and execution on unqualified platforms;
4. preserves Stage-D MCP/tool mediation, Stage-F immutable context and scoped memory, Stage-G exact
   package pinning, Stage-I install handoff and Stage-H secret/account/approval/trust fencing;
5. gives operators accurate UI guidance about Worker availability and package-sandbox support;
6. completes security, migration, regression and integrated demo verification before changing the
   authoritative implementation status.

Linux with bubblewrap is the first intended qualification target. Windows Job Objects and POSIX
rlimits remain resource-control primitives and must not be advertised as filesystem/network
isolation. Windows and macOS must fail closed until separately qualified. Docker or WSL may be used
as a deterministic Linux test environment, but Docker must not become the NervOS production package
execution path.

## Read before changing code

Read these files in this order. Do not rely on roadmap wording as proof of delivered behavior.

### Project and delivered-state authority

1. `AGENTS.md`
2. `docs/implementation-status.md`
3. `docs/architecture.md`
4. `docs/runtime.md`
5. `docs/permissions.md`
6. `docs/roadmap.md`

### Frozen and successor security decisions

7. `docs/stage-h/README.md`
8. `docs/stage-h/verification-review-2026-10-03.md`
9. `docs/stage-h/next-implementation-plan-2026-10-04.md`
10. `docs/adr/0032-encrypted-secret-manager.md`
11. `docs/adr/0033-account-connections-and-credential-brokering.md`
12. `docs/adr/0034-per-action-approval.md`
13. `docs/adr/0035-package-sandbox-and-resource-controls.md`
14. `docs/adr/0036-publisher-trust-and-local-revocation.md`
15. `docs/adr/0037-stage-h-security-repair-contract.md`
16. `docs/adr/0038-pre-exec-package-isolation.md`

ADR 0037 supersedes contradictory Stage-H completion claims and requires real pre-exec filesystem
and network denial. ADR 0038 defines the approved Linux-first launch direction. If implementation
evidence shows ADR 0038 is insufficient or conflicts with a frozen invariant, stop that part and
report `STAGE H ARCHITECTURE CHANGE REQUEST — <specific issue>` rather than silently weakening it.

### Predecessor contracts that must not regress

17. `docs/stage-f/README.md` and ADRs 0021–0023 if context or memory code changes. Run
    `git hash-object docs/stage-f/README.md` and report the Stage-F plan blob before such changes.
18. `docs/stage-g/README.md` and ADRs 0024–0026
19. `docs/stage-i/README.md` and ADRs 0027–0030
20. `docs/adr/0031-runtime-integration-context-memory-and-mcp.md`
21. `docs/runtime-integration/README.md`
22. `docs/agent-packages.md`
23. `docs/configuration.md`
24. `docs/database.md`

### Runtime and containment implementation

25. `packages/nervos-core/src/nervos_core/application/sandbox.py`
26. `packages/nervos-core/src/nervos_core/infrastructure/sandbox/__init__.py`
27. `packages/nervos-core/src/nervos_core/infrastructure/sandbox/posix.py`
28. `packages/nervos-core/src/nervos_core/infrastructure/sandbox/windows.py`
29. `packages/nervos-core/src/nervos_core/infrastructure/sandbox/linux_bubblewrap.py`
30. `apps/worker/src/nervos_worker/package_execution.py`
31. `packages/nervos-core/src/nervos_core/application/package_health.py`
32. `packages/nervos-core/src/nervos_core/application/package_installation.py`
33. `apps/api/src/nervos_api/app.py`
34. `apps/worker/src/nervos_worker/app.py`
35. `apps/worker/src/nervos_worker/service.py`
36. `apps/scheduler/src/nervos_scheduler/app.py`
37. `packages/nervos-package-host/src/nervos_package_host/__main__.py`
38. `packages/nervos-package-host/src/nervos_package_host/wire.py`
39. `packages/nervos-sdk/src/nervos_sdk/types.py`

### Security implementation

40. `packages/nervos-core/src/nervos_core/application/secrets.py`
41. `packages/nervos-core/src/nervos_core/application/account_oauth.py`
42. `packages/nervos-core/src/nervos_core/application/account_actions.py`
43. `packages/nervos-core/src/nervos_core/application/approvals.py`
44. `packages/nervos-core/src/nervos_core/application/tool_invocation_mediator.py`
45. `packages/nervos-core/src/nervos_core/infrastructure/security/secret_keys.py`
46. `packages/nervos-mcp/src/nervos_mcp/account_tokens.py`
47. `packages/nervos-mcp/src/nervos_mcp/gateway.py`
48. `apps/worker/src/nervos_worker/account_actions.py`
49. `apps/api/src/nervos_api/api/routes/security.py`
50. `scripts/secret_maintenance.py`

### Runtime health and operator UI

51. `packages/nervos-core/src/nervos_core/application/runtime_integration.py`
52. `packages/nervos-core/src/nervos_core/infrastructure/database/runtime_integration.py`
53. `apps/api/src/nervos_api/api/routes/runtime_integration.py`
54. `apps/web/src/api/runtimeIntegration.ts`
55. `apps/web/src/pages/RuntimeHealthPage.tsx`
56. `apps/web/src/pages/SecurityPage.tsx`
57. `apps/web/src/pages/ConnectionsPage.tsx`
58. `apps/web/src/components/RuntimeControls.tsx`
59. `apps/web/src/router.tsx`

### Tests and acceptance journeys

60. `packages/nervos-core/tests/integration/test_stage_h_sandbox.py`
61. `packages/nervos-core/tests/integration/test_stage_h_key_rotation.py`
62. `packages/nervos-core/tests/integration/test_stage_h_account_oauth.py`
63. `packages/nervos-core/tests/integration/test_stage_h_account_dispatch.py`
64. `packages/nervos-core/tests/integration/test_stage_h_approval_resumption.py`
65. `packages/nervos-core/tests/integration/test_stage_h_security.py`
66. `packages/nervos-core/tests/integration/test_package_lifecycle_mutations.py`
67. `apps/api/tests/integration/test_stage_h_routes.py`
68. `apps/api/tests/integration/test_runtime_integration_routes.py`
69. `apps/api/tests/integration/test_migrations.py`
70. `apps/api/tests/integration/test_migrations_d1.py`
71. `apps/worker/tests/integration/test_stage_g5_real_runtime.py`
72. `apps/marketplace/tests/integration/local_runtime_journey.py`
73. `apps/web/src/pages/RuntimeHealthPage.test.tsx`
74. `apps/web/src/pages/SecurityPage.test.tsx`
75. `apps/web/src/components/RuntimeControls.test.tsx`
76. `scripts/e2e.py`

Also inspect `git status --short`, `git diff --check`, the current migration graph and all applicable
nested `AGENTS.md` files before editing. Never read `.env`, `.env.*`, private keys, credentials,
`secrets/` or the user's live NervOS database.

## Current worktree facts to verify, not blindly trust

- The worktree is intentionally dirty and contains Priority-1 runtime integration and Stage-H repair
  work. Preserve it.
- Local migration head is intended to be `0022_stage_h_account_oauth`; Worker and Scheduler guards
  should match it. Hosted Marketplace migrations remain independent.
- A fresh full suite previously reached `495 passed` before finding one stale Marketplace
  architecture assertion that still expected migration `0020`. That assertion was updated to
  `0022` and its focused test passed. A second full suite was interrupted and is not acceptance.
- Focused runtime integration and sandbox tests most recently produced `23 passed, 3 skipped` on
  Windows. The three skips are Linux/POSIX evidence and must not be counted as passing there.
- The new runtime-health frontend test passed after its loading-state assertion was corrected.
- A real Linux WSL probe using bubblewrap produced `tier=full`, denied a protected sibling file,
  denied a direct connection to `1.1.1.1:53`, allowed private scratch writing and killed a spawned
  descendant before it could write a delayed marker. Convert this ad hoc evidence into reproducible
  repository acceptance; do not rely on the prior terminal output alone.
- Docker Desktop's normal engine was unavailable during that probe. The `docker-desktop` WSL
  environment was reachable and received temporary `python3` and `bubblewrap` packages. Recheck
  prerequisites; do not assume that environment persists.

## Implementation order

### 1. Establish and repair the baseline

Run the full Python suite with first-failure output against isolated test databases. Fix each real
failure in order. Classify failures as behavior defects, outdated fixtures, or contract conflicts.
Never delete or weaken a test merely to pass it. After focused fixes, rerun the affected suite and
eventually the complete suite without `-x`.

Verify:

- Alembic has one local head, expected `0022_stage_h_account_oauth` unless a justified new migration
  is added.
- Fresh install and upgrade from supported prior revisions work.
- SQLAlchemy metadata matches migrations.
- Worker and Scheduler schema guards match the real local head.
- Marketplace schema and revisions remain separate.

Do not update completion wording yet.

### 2. Finish the pre-exec sandbox contract

Review the partial `SandboxLaunch` and `LinuxBubblewrapContainment` implementation. Correct it rather
than creating a second launcher.

Required behavior:

- Resolve containment before spawning.
- Production `create_containment()` returns a launcher only on qualified Linux with `bwrap` present.
- Bubblewrap must create user, mount, PID and network namespaces, drop capabilities and clear the
  inherited environment.
- Mount only required read-only system/runtime roots and the exact immutable package environment.
- Mount exactly one attempt-specific scratch directory writable.
- Do not mount user home, database, package-store siblings, secret keys, configuration, another
  package environment or another attempt's scratch.
- Preserve only bounded safe locale/runtime environment values. Never forward arbitrary host
  environment variables.
- Apply frozen memory, CPU, process, descriptor and file-size limits before exec.
- Track and terminate the complete process group on cancellation, timeout, lease loss, protocol
  failure and Worker shutdown.
- Keep stdout/stderr and frame limits enforced. A cap violation must terminate the sandbox.
- Unsupported, missing or failed isolation must return the static safe refusal. There is no
  production bypass.

Important partial-code defect to resolve: the current qualified package health checker passes the
package environment itself as `scratch`. That can remount the immutable environment writable. Give
every health check a separate private temporary scratch directory and clean it on every outcome.
Use the same pre-exec launcher as normal execution.

Resource-only `WindowsJobObjectContainment` and `PosixContainment` may remain explicit trusted-test
fixtures. They must never be returned by the production factory and must never report `FULL`.

### 3. Make sandbox capability reporting authoritative

The current partial runtime-health implementation computes sandbox capability in the API process.
That is insufficient because the Worker is the package executor and may run on a different host.

Implement a Worker-reported, durable capability projection:

- Worker registration/heartbeat advertises only a bounded public capability such as
  `{package_execution_supported, platform, backend}`.
- Treat capability as observed health, not execution authority. The launch factory still decides at
  every package start.
- Expired Workers do not count as available.
- Aggregate safely for the signed-in owner without exposing Worker IDs, hostnames, paths, fleet
  topology, other users' workload or credentials.
- If persistence changes, add a new Alembic revision after `0022`; update metadata, migration tests,
  Worker/Scheduler guards and documentation together.
- Remove or replace API-host capability reporting once Worker reporting is authoritative.

The Runtime Health UI must distinguish:

- built-in agent execution availability;
- qualified package-execution availability;
- unsupported platform or missing sandbox prerequisite;
- queued/running/retrying owner workload.

Use actionable, static messages. Do not display raw exceptions or filesystem paths.

### 4. Complete Stage-H security qualification

H1:

- Test current/old key resolution by ciphertext key version.
- Test interrupted bounded re-encryption and safe idempotent restart.
- Refuse retirement of the active key or a key referenced by recoverable ciphertext.
- Verify revoked secrets cannot resolve.
- Verify operator key-file permissions on Linux and Windows, or document and enforce a precise
  fail-closed operator requirement. Never print key material.

H2:

- Test PKCE S256 state ownership, expiry, one-use behavior and replay refusal.
- Test failed/malformed/oversized token responses and redirect refusal.
- Test bounded refresh single-flight, stale CAS refusal, disconnect during refresh and scope
  non-widening.
- Test provider revocation success/failure/not-supported outcomes.
- Test live account ownership, connection state, tool fingerprint, Stage-D grant, exact Run/Attempt
  fencing and required scope immediately before dispatch.
- Prove tokens never enter package messages, model prompts, browser responses, audit events or logs.

H3:

- Test one exact action digest, tool fingerprint, Run and Attempt binding.
- Test approve, deny, expiry, cancellation, grant revocation, lease loss and Worker restart.
- Approval may resume only the same still-live Attempt and invocation.
- Approval consumption and invocation start must be one transaction after live checks.
- Prove zero or one external dispatch and no model-call replay.
- Improve action previews only with a reviewed bounded redaction contract.

H5:

- Test install, rebind, rollback, claim and execution admission against live publisher trust.
- A revoked publisher blocks new authority while historical executable evidence remains readable.
- Marketplace installation must still hand off to the existing Stage-G verifier and installer.

### 5. Complete dashboard behavior

Use the existing routes and components. Do not build a second dashboard.

- Runtime Health: authoritative Worker/sandbox capability, workload counts and clear refusal help.
- Security: write-only secrets, account authorization/reconnect/disconnect/revocation status,
  pending approvals and publisher trust.
- Connections: MCP health, discovered tools and safe refresh/enable/disable behavior.
- Agent page: portable tool requirements, explicit binding, grant/revoke/reconfirm, invocation
  history, memory policy and memory suggestions.
- Run timeline: approval-requested/decided and static safe failure events.
- Context panel: immutable current input, selected memory versions and bounded history.

Every screen needs loading, success, empty, validation, unauthorized/foreign, conflict and service
failure coverage as applicable. Preserve keyboard access, labels, focus behavior and clear status
announcements.

### 6. Reproducible Linux qualification and integrated acceptance

Add a repository-owned Linux qualification command or test target. It must use the production
launcher, not a mock, and prove:

1. protected host file cannot be read;
2. direct outbound connection fails;
3. scratch is writable;
4. package environment is read-only;
5. another scratch/environment is unavailable;
6. spawned descendants die on termination;
7. resource and aggregate-output limits bind;
8. a normal signed package completes through package-host IPC;
9. mediated model and granted MCP tool calls still work;
10. an ungranted tool remains denied;
11. context and selected memory reach the SDK through the reviewed structured contract;
12. memory proposals follow manual/review/automatic-private policy without granting authority.

If Linux qualification runs in Docker/WSL, document the exact prerequisite and command. Do not call
it native-host qualification. Windows tests must prove clear refusal before package import while
trusted built-in agents continue to work.

Run a deterministic integrated journey covering signed install, AgentInstance creation, explicit
tool binding/grant, execution, approval, context inspection, memory review/retrieval, cancellation,
Worker restart, provider failure, upgrade, rollback and revoked-publisher refusal. Never call a real
AI provider in automated acceptance; use deterministic fakes.

### 7. Documentation and truthful closeout

Reconcile contradictory Stage-H wording in:

- `docs/implementation-status.md`
- `docs/roadmap.md`
- `docs/stage-h/README.md`
- `docs/runtime-integration/README.md`
- `docs/runtime.md`
- `docs/permissions.md`
- `docs/configuration.md`
- `docs/database.md`
- `.env.example` using fake placeholders only

Do not rewrite frozen historical decisions. Add successor notes where appropriate. State exactly:

- which platform/backend is qualified;
- what prerequisites it needs;
- which platforms refuse;
- what was tested natively versus in WSL/container qualification;
- what remains Stage-I production/OIDC acceptance;
- that durable LangGraph/framework checkpoints and universal SDK compatibility remain future work.

Only mark Stage H complete after all applicable gates below pass and the evidence is recorded.

## What not to do

- Do not run arbitrary packages unsandboxed as a fallback.
- Do not advertise Windows Job Objects or rlimits as full isolation.
- Do not attach containment only after arbitrary package code has already started.
- Do not give package code direct network access, raw account tokens, model credentials, database
  access or host filesystem access.
- Do not bypass Stage-D grants/MCP mediation or Stage-H action approval.
- Do not merge memory with permissions or use memory as trusted instructions.
- Do not automatically share all memory between agents.
- Do not introduce a second Run/Job queue or execute inside the API process.
- Do not add Redis, Kafka, Kubernetes or a microservice split.
- Do not make Docker the production package runtime without a new explicit architecture approval.
- Do not read or modify real `.env`, credentials, secret files or the user's live database.
- Do not hard-code, log, test with or document real credentials.
- Do not weaken tests, hide skips, suppress failures or call focused tests full acceptance.
- Do not modify hosted Marketplace migrations when changing the local runtime schema.
- Do not implement Stage-J shared memory/inter-agent handoff or durable autonomous workflow
  checkpoints as part of this Stage-H closeout.
- Do not commit or perform Git finalization unless separately requested.

## Required verification commands

Use repository commands where available. Run narrow tests after each change, then run all applicable
gates. At minimum:

```powershell
uv run pytest packages/nervos-core/tests/integration/test_stage_h_sandbox.py -q
uv run pytest packages/nervos-core/tests/integration/test_stage_h_key_rotation.py -q
uv run pytest packages/nervos-core/tests/integration/test_stage_h_account_oauth.py -q
uv run pytest packages/nervos-core/tests/integration/test_stage_h_account_dispatch.py -q
uv run pytest packages/nervos-core/tests/integration/test_stage_h_approval_resumption.py -q
uv run pytest packages/nervos-core/tests/integration/test_stage_h_security.py -q
uv run pytest packages/nervos-core/tests/integration/test_package_lifecycle_mutations.py -q
uv run pytest apps/api/tests/integration/test_stage_h_routes.py -q
uv run pytest apps/api/tests/integration/test_runtime_integration_routes.py -q
uv run pytest apps/api/tests/integration/test_migrations.py apps/api/tests/integration/test_migrations_d1.py -q
uv run pytest apps/worker/tests/integration/test_stage_g5_real_runtime.py -q
uv run pytest -q --tb=short
pnpm --dir apps/web test -- --run
pnpm --dir apps/web lint
pnpm --dir apps/web build
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run python scripts/security_scan.py
git diff --check
```

Also run `make check`, `make clean-check` and the repository E2E command if those targets are current
and do not duplicate a known destructive action. Run the Linux qualification command on an actual
Linux kernel with `bwrap`; record skipped evidence explicitly on Windows. Run supervised API,
Worker, Scheduler and Web startup using isolated temporary data and fake providers, then execute the
documented demo journey.

Do not read the user's live configuration to start services. Use test settings, temporary database,
temporary package store, fake OAuth/token endpoints and fake model/MCP providers.

## Completion report

At the end report:

1. exactly what changed and why;
2. architecture and security invariants preserved;
3. migrations added and upgrade/downgrade evidence;
4. supported and refused platforms;
5. every test/check command with pass/fail/skip counts;
6. native versus WSL/container qualification evidence;
7. supervised startup and integrated demo results;
8. remaining risks or external acceptance items;
9. files changed, without including secret values;
10. confirmation that no commit/PR/merge occurred unless separately authorized.

If any required gate fails, fix it and rerun it. If it cannot be fixed within the accepted
architecture, report the exact blocker and leave implementation status open. Never state “complete”
while required evidence is missing.

---
