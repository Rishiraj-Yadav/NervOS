# Next implementation plan — runtime qualification and Stage-H closeout

Prepared: 2026-10-04. Status: approved for implementation by the user on 2026-10-04;
this document does not by itself declare implementation complete.

## Findings from the current worktree

- Existing stages supply durable Run/Job/Attempt execution, concurrency, scheduling,
  conversations, scoped memory, package installation and Marketplace MVP integration.
- ADR 0031 integration code and dashboard surfaces exist for structured package context,
  selected memory, memory proposals/policies, MCP bindings/grants and runtime health.
- Stage-H repair code exists for versioned encryption keys, PKCE account authorization,
  credential brokering, same-Attempt action approval and publisher revocation checks.
- `create_containment()` currently always raises `ContainmentUnavailable`. Production
  package execution is therefore refused; resource adapters are not qualified isolation.
- `PackageContainment.establish(process_id)` assumes an already-started child. A real
  launcher must establish protection before arbitrary package code executes, as required
  by ADR 0037. Merely enabling the existing factory would violate that requirement.
- Stage-H completion wording conflicts across implementation-status, roadmap, master
  plan and runtime-integration documentation. ADR 0037 supersedes contradictory claims.
- The pytest cache contains 104 failed test identifiers. This is historical evidence,
  not a fresh test result or proof of 104 current defects. Full acceptance must be rerun.
- The SDK integration guide explicitly does not promise durable framework checkpoints
  or universal LangChain/LangGraph compatibility.

## Goal

Deliver a reproducibly verified runtime that can safely install and execute a package
on one explicitly supported platform, preserve existing model/tool/context/memory
boundaries, and expose clear operator guidance on unsupported platforms.

## Implementation order

### 1. Establish the actual baseline and repair regressions

Run the current backend suite against isolated test databases; capture failure traces
and classify behavior defects, outdated fixtures and architecture-contract conflicts.
Repair defects and update expectations only where an approved contract changed.
Retain coverage and inspect previously accepted behavior for regressions.

Verify migrations through local head `0022_stage_h_account_oauth`, upgrade from earlier
supported heads, metadata parity and matching Worker/Scheduler guards. Keep hosted
Marketplace migrations independent. Reconcile delivered-state documentation using
actual evidence without silently rewriting frozen plans.

Acceptance: reproducible backend/frontend, lint, types, build, security and migration
checks; explicit platform skips and outstanding acceptance items recorded accurately.

### 2. Freeze the protected-launch contract

Prepare a successor ADR defining launch ownership, supported OS/backend, available
prerequisites, filesystem mounts, network denial, resource limits, process-tree
termination, scratch cleanup, output bounds and qualification evidence.

Recommend Linux as the first qualification target, subject to backend feasibility.
The frozen plan excludes Docker; a container backend would require an explicit
architecture change. Windows users must receive a documented supported deployment
route and an honest unsupported-local-runtime indication until Windows is qualified.

Preserve Worker authority, immutable executable snapshots and SDK host IPC. Keep
credentials, the database and host configuration outside the package boundary.
Package model and tool access continues exclusively through NervOS mediation.

Acceptance: reviewed launch contract and threat/test matrix before enabling a backend.

### 3. Implement and qualify one real isolation backend

Change launch composition so isolation is active before package imports or execution.
Allow only the required read-only runtime/package files and private writable scratch.
Deny host protected files, direct network access and access to other packages' scratch.
Apply process/memory/CPU/output bounds and terminate descendants on cancellation,
timeout, lease loss and launch failure. Reuse this protected launch for installation
health checks as well as execution. Keep unsupported platforms refused.

Acceptance: real OS tests using hostile packages prove file/network denial, subprocess
containment, cleanup and resource limits. Positive tests prove model/tool IPC, context,
memory and normal signed package execution through the same protected launcher.
Explicit test doubles do not count as platform qualification.

### 4. Complete security behavior and dashboard qualification

Exercise OAuth state expiry/replay, failed exchanges, bounded token responses, refresh
races, disconnect/revoke during dispatch and stale credential fencing. Verify key
rotation interruption/recovery, safe retirement and operator key-file protection.
Exercise exact-action approval, expiry, denial, cancellation, lease loss and restart.
Preserve the current contract: approval does not resume a terminated Attempt.

Extend existing Runtime health, Security, Connections and agent controls to show
supported sandbox capability, configuration problems, account reconnection/revocation
state, actionable approval status and clear launch refusal guidance. Do not expose
tokens, raw exceptions or other owners' activity. Improve action previews only with
an explicitly reviewed, bounded redaction contract.

Acceptance: deterministic API and browser journeys for authorization, permission
grant/revoke, approval/resumption, account disconnection and sandbox refusal.

### 5. Prove the integrated runtime and close out

Run a supervised demo: signed package installation, instance configuration, explicit
MCP binding/grant, execution, selected context inspection, memory proposal/review and
later retrieval, dangerous action approval, cancellation and recovery. Exercise
multiple agents concurrently, provider failure and Worker restart. Verify upgrades,
rollback, revoked-publisher refusal and pinned historical Run evidence.

Acceptance: complete automated gates plus a reproducible operator runbook and actual
startup evidence. Record which platforms and SDK/model/tool combinations were tested.
Claim completion only for the qualified scope. Marketplace production/OIDC acceptance
remains a separate Stage-I obligation.

## Expected code ownership

- `packages/nervos-core`: application launch contracts, live authority and persistence.
- `packages/nervos-core/.../infrastructure/sandbox`: concrete OS containment adapters.
- `apps/worker`: protected launch, lifecycle cleanup, model/tool/account composition.
- `apps/api`: authenticated capability/status projections and operator controls.
- `apps/web`: existing health/security/connection/agent surfaces and browser journeys.
- `packages/nervos-sdk` and `packages/nervos-package-host`: protocol compatibility;
  change only if protected launch requires a reviewed additive contract.
- `docs` and existing test suites: evidence, supported-platform matrix and runbook.

Schema changes, if needed, use new Alembic revisions; no manual schema edits. Preserve
the modular monolith and existing Run → Job → Attempt → Worker authority throughout.

## Following milestone, after this closeout

Prepare a separate autonomous-workflow architecture plan covering durable application
state/checkpoints, bounded event-driven executions, external-action idempotency and
ambiguity, restart recovery and a tested SDK adapter. Then build one read-first account
connector and example workflow; enable approved mutations after recovery tests pass.
Agent-private memory remains distinct from workflow checkpoints. Shared memory and
inter-agent handoffs require Stage-J governance and explicit authorization.

No implementation or verification commands were run as part of preparing this plan;
the review used documentation, source code, the existing graph and cached test evidence.
