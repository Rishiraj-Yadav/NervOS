# Stage H verification and repair request — 2026-10-03

> **Repair completion update — 2026-10-04.** The findings below describe the pre-repair audit.
> The user approved ADR 0037 and the implementation now provides versioned secret re-encryption,
> OAuth PKCE/token brokering, same-Attempt approval resumption and atomic consumption, explicit
> package-platform refusal, private scratch/output bounds, and live publisher-trust rebind checks.
> The current migration head is `0022_stage_h_account_oauth`. No platform has qualified combined
> filesystem/network isolation, so package execution is intentionally refused everywhere until a
> qualified launcher and hostile-package probes are available. See `implementation-status.md` for
> the authoritative delivered state.

## Finding

Stage H is **not complete**. This review compared the frozen master plan, ADRs
0032–0036, implementation status, implementation code and focused tests. Existing
completion claims exceed the behavior delivered. The frozen master plan and accepted
ADRs have not been silently amended. The implementation-status document now records
the gaps below. This review is an acceptance audit, not a claim of live deployment
or complete production qualification.

## Requirement coverage

| Milestone | Delivered | Missing or incorrect |
|---|---|---|
| H0 | Master plan and ADRs exist. | H2 OAuth scope and H3 resumption/approval scope conflict with implementation and closeout descriptions. Resolve explicitly before new acceptance. |
| H1 | AES-GCM storage, metadata-only/write-only API, external key resolver. | Resolver provides one current key rather than resolving each row's key version. Activating key metadata does not re-encrypt ciphertext. No complete re-encryption and safe key-retirement lifecycle. |
| H2 | Owner-scoped account records, secret references and disconnect state. | CredentialBroker is not wired into execution dispatch; no Agent/Run/live-grant check in broker. ADR 0033's OAuth PKCE flow, token refresh and server-side authenticated dispatch are absent. |
| H3 | Durable approval records, owner decision API and mediator gate. | Pending calls are denied immediately; there is no working owner-decision/resume journey. Matching omits Attempt identity, consumption discards consuming Attempt, and consumption is separate from the invocation-start transaction. Approval events required by ADR 0034 are absent. |
| H4 | Windows Job Object resource controls, POSIX rlimit adapter, macOS refusal. | These adapters do not enforce required filesystem/network deny rules. POSIX checks cgroup availability but does not create/configure/attach a cgroup. Windows attaches an already running process. Scratch is shared by environment, not unique per execution. Aggregate output limit is not enforced. |
| H5 | Persistent trust, install and Worker checks. | Target rebind/rollback trust check was missing and is repaired in this audit. Full integration and revised security closeout still require acceptance. |

## Implementation evidence

Paths below are repository-relative for portability.

- H1: `packages/nervos-core/src/nervos_core/application/secrets.py`,
  `infrastructure/security/secret_keys.py` and `infrastructure/database/secrets.py`.
  The latter two paths are under the same `nervos_core` source directory. The
  resolver caches one key; activating a key version alone cannot preserve access
  to old ciphertext encrypted under another key.
- H2: `packages/nervos-core/src/nervos_core/application/account_connections.py`.
  CredentialBroker is declared and tested but not composed into a production
  connector dispatch path. Account management does not establish that an agent
  can securely perform authenticated external operations.
- H3: `packages/nervos-core/src/nervos_core/infrastructure/database/approvals.py`
  and `application/tool_invocation_mediator.py`. `_matching` lacks Attempt identity;
  `consume` discards `consuming_attempt_id`; approval consumption commits separately
  from `mark_started`. Requested approvals return denial immediately. A next Run
  cannot consume a decision bound to the previous Run, so approval for a next Run
  does not resolve this lifecycle gap.
- H4: `packages/nervos-core/src/nervos_core/infrastructure/sandbox/windows.py`,
  `posix.py`, and `apps/worker/src/nervos_worker/package_execution.py`. Resource
  limits and a clean child environment do not prevent same-user package code
  from opening host files or sockets. Reporting `FULL` containment is not proof
  of the master plan's filesystem/network guarantees. Existing platform tests
  exercise resource adapters, not protected-file/network-denial probes.
- H5: `packages/nervos-core/src/nervos_core/application/package_installation.py`.
  `rebind_instance` now checks target signer trust before changing the binding.
  The same method handles explicit target-version rollback.

## Repair completed without changing architecture

Added the missing H5 target signer check using the existing `ensure_installable`
authority. Added a lifecycle regression that revokes an installed signer, attempts
rebind, expects `PublisherRevoked`, and verifies that the original binding remains
unchanged. The test fixture composes the existing trust service against an isolated
migrated database. No migration, Git finalization or real credential access occurred.

## Architecture-change request

**STAGE H ARCHITECTURE CHANGE REQUEST — complete account, approval and isolation contracts**

The master plan section 1 requires stopping on conflict with the plan or accepted
H ADRs. Section 9 requires recording conflict, affected ADRs, resolution,
compatibility/migration impact and tests before architectural implementation.

Conflicts requiring an explicit reviewed resolution:

1. ADR 0033 requires account OAuth PKCE while closeout text excludes OAuth and the
   implementation only manages account records. Restore H2's promised scope through
   an explicit provider contract, authorization-code PKCE S256, server-side token
   persistence/refresh and connector dispatch integration. Enforce owner, Agent,
   Run, scopes, live permission and connection status at dispatch. Raw credentials
   must never enter package RPC, prompts, memory, events or browser responses.
2. ADR 0034 binds approval to an exact Run/Attempt/action but describes approval for
   a next Run after denial. Replace this contradictory lifecycle in a successor
   ADR with a defined durable pending/resumption model. Specify lease release or
   retention, cancellation/expiry, Worker restart, no model/effect replay, and
   transactionally fenced consumption together with invocation start. Add the
   two public approval event types under the existing Stage-D safe audit boundary.
3. The master plan requires OS filesystem/network isolation; ADR 0035 and current
   resource adapters do not deliver it. Publish a successor platform contract with
   actual enforced boundaries and honest unsupported-platform refusal. Qualify
   each supported OS before enabling untrusted package execution there. Define
   child creation/attachment without an escape window, descendant termination,
   private per-execution scratch cleanup/quotas and bounded stdout/stderr.
4. ADR 0032 key lifecycle needs version-addressable key resolution, resumable
   explicit re-encryption and refusal to retire referenced keys. Preserve old
   ciphertext readability during interruption and rotation.

Preserve core dependency direction, Run → Job → Attempt authority, Stage-D tool
mediation, Stage-F immutable context/memory rules, Stage-G pinned package evidence
and Stage-I installer handoff. Do not make arbitrary packages trusted by default.

Compatibility and schema impact: keep existing secret/account/trust records;
introduce explicit schema changes with Alembic only where needed for key lifecycle,
approval waiting/fencing or the public event vocabulary. Allocate revisions from
the actual local head `0020_stage_h5_publisher_trust`; update Worker/Scheduler guards
and migration acceptance together. Hosted Marketplace migrations stay independent.
Existing weak sandbox platforms may have to refuse execution until qualified;
document that behavior change before rollout.

## Proposed implementation and acceptance order

1. Review successor ADRs and freeze supported platforms and approval resumption.
2. Complete H1 key lifecycle and H2 broker/OAuth integration with isolated fake
   providers, scope/revocation/refresh-race tests and secret-redaction checks.
3. Complete H3 pending/decision/resume, expiry/cancel/lease fencing, atomic start
   and safe audit events; prove the real dashboard decision journey.
4. Complete H4 actual OS isolation and output/scratch/descendant controls; run
   malicious-package probes against protected files and direct network access
   on each supported platform. Refuse execution where isolation cannot be established.
5. Qualify H5 install/rebind/rollback/claim revocation and compatibility through
   the existing Stage-G and Stage-I entry points.
6. Run backend/frontend, lint/type/security/migration gates, deterministic E2E,
   concurrent/restart/cancel/provider-failure tests and supervised demo startup.
   Update acceptance only after these requirements pass.

## Verification performed in this audit

- Focused H security, approval gate, sandbox and API suites: **33 passed, 2 skipped**.
  Both skips are POSIX-only tests on this Windows host; Linux acceptance is not inferred.
- Package lifecycle mutation suite including revoked-target regression: **5 passed**.
- Ruff lint and format checks on the two changed Python files passed.
- Repository-wide Pyright passed with **0 errors and 0 warnings**.
- The previous complete-suite counts remain historical evidence only. Passing
  narrow existing tests does not satisfy absent behavioral/security requirements.

No claim is made that a live NervOS instance or Marketplace has been started or
that untrusted package execution is currently safe. Closing these requirements
needs the reviewed architectural resolution above and its acceptance evidence.
