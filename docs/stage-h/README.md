# NervOS Stage H — Security Isolation (Master Plan)

## 1. Status and verification protocol

**H0 — the architecture freeze in this document and ADRs 0032–0036 — is implemented by explicit
user authorization on 2026-10-03, together with the H1–H5 implementation milestones it governs.**
Every Stage-H implementation milestone MUST read this plan and the accepted ADRs 0032–0036
before changing code. Baseline: uncommitted Priority-1 runtime integration work on `main`
(head `2dd25a5`), preserved in place; local migration head entering Stage H is
`0015_runtime_integration`; the frozen predecessor blobs are F
`62cfc0a8039e233c82e2a30ff3fd59495e99395d`, G `4554ab5e93b35f4bbc4daed164ee5712077c1006`.

A conflict with this plan or an accepted Stage-H ADR is a stop condition:

```text
STAGE H ARCHITECTURE CHANGE REQUEST — <issue>
```

Do not silently edit the frozen plan while implementing. H0 delivered no runtime behavior by
itself; H1–H5 deliver it under this contract.

**Milestone status: H0 through H5 are all delivered, and Stage H is COMPLETE.** The local migration
head is `0020_stage_h5_publisher_trust`. See section 10 for what was verified on which platform and
what remains genuinely open. **Successor note (2026-10-04):** the 2026-10-03 verification review
reopened this claim, and ADR 0037/0038 replaced the resource-only H4 with a pre-exec Linux
bubblewrap launcher. The current head is `0023_worker_sandbox_capability`; section 13 records the
qualified platform, the qualification command and the evidence. Sections 11 and 12 below describe
the state at head `0020` and are kept as the historical record.

## 2. Purpose

Stage H makes it safer to run untrusted `.nervos` package code and to connect agents to external
accounts and tools. It delivers five capabilities the roadmap assigns to no other stage:

1. **Encrypted secret storage** — owner-scoped, key-managed, never displayed again (H1).
2. **Account connections and credential brokering** — server-side credential resolution, tokens
   never reaching packages, models, or browsers (H2).
3. **Per-action approval** — durable Ask/Approve/Deny bound to one exact action (H3).
4. **Real OS-enforced sandboxing** for package processes with resource, filesystem, and network
   controls — or honest refusal to run when isolation is unavailable (H4).
5. **Persistent publisher trust and local revocation enforcement** connected to the Stage-G
   installer and the Stage-I distribution observations (H5).

Stage H deliberately does **not** touch hosted Marketplace production acceptance, OIDC, or
external publication authority: those remain Stage I responsibilities. ADR 0031's runtime
integration behavior must not regress.

## 3. Threat model

### Protected assets

| Asset | Owner | Where it lives |
|---|---|---|
| Secret values (provider keys, connector tokens, webhook credentials) | user | Secret Manager (encrypted at rest, key outside DB) |
| Account connection grants (scopes, expiry, refresh state) | user | `account_connections` + Secret Manager |
| Approval decisions (who approved what, when) | user | `action_approvals` (durable audit) |
| The NervOS database, config, and package store | operator | host filesystem |
| Run history, conversations, memory | user | SQLite (existing integrity guarantees) |
| Model-provider credentials | operator | Worker process environment only (unchanged) |

### Trust boundaries

```text
Browser ── opaque session ── API (control plane)
Worker ── lease-fenced claim ── Run/Job/Attempt authority
Worker ── bounded wire protocol ── package-host subprocess   ← hardened in H4
Package code ── SDK ports ── mediator (Stage D) + broker + approvals (H2/H3)
Secret Manager key ── never enters: DB, packages, logs, API responses, model prompts
```

### Attacker capabilities and defenses

| Attacker | Capability | Stage-H defense |
|---|---|---|
| Hostile package code | read files, spawn processes, open sockets, exhaust resources | OS job/object containment (H4), default-deny network, filesystem allowlist, resource caps; refuse to run when containment is unavailable |
| Hostile package code | steal another package's or the host's credentials | credentials never cross the package boundary; broker resolves them server-side (H2); package env is minimal |
| Compromised model / prompt injection | request a dangerous tool action | grants still decide (Stage D); dangerous actions additionally require per-action owner approval (H3); memory is never authorization |
| Malicious publisher | distribute signed malware | signature ≠ trust; persistent local trust store, explicit trust decisions, revocation enforcement (H5) |
| Local malware (OS user level) | read the database directly | out of scope — documented residual risk; same user running arbitrary code defeats any in-process defense |
| Network attacker | egress from a package | default-deny network for packages; mediated model/tool/connector calls only |

### Explicit non-goals

- No protection against an attacker running as the same OS user outside the sandbox.
- No production Marketplace, OIDC, or hosted publisher operations (Stage I).
- No exactly-once external actions; the ADR 0017 ambiguity contract is unchanged.
- No universal SDK compatibility claim; packages must target the supported host protocol.
- No Docker/Kubernetes/Redis/Kafka; OS primitives only (ADR 0035).
- No secret sharing between users; no package-readable secret enumeration API.

## 4. Composition with the existing execution model

Stage H adds **gates** on the existing path; it adds no second execution engine:

```text
Trigger → Run → Job → Worker → Attempt
    → [H4] package-host under OS containment (or refuse)
        → SDK tool request → ToolInvocationMediator (Stage D, unchanged checks)
            → [H3] approval gate (new) → [H2] credential broker (new) → executor
```

- The Worker remains the sole authority for claim, lease, retry, cancel, timeout.
- The Stage-D mediator remains the sole tool authority; H3 wraps it, never bypasses it.
- The package-host wire protocol (`wire.py`) gains **no new authority**: package code still
  cannot request arbitrary host actions. H4 hardens the process around it.
- Runs/Attempts keep immutable snapshots; a grant, connection, or approval created after a Run's
  snapshot never retroactively authorizes it (grant cutoff semantics extended in ADR 0034).

## 5. Ownership and dependency direction

```text
apps/api          → nervos-core (secrets, connections, approvals, trust services)
apps/worker       → nervos-core (broker resolution, approval gate, sandbox launcher, trust checks)
packages/nervos-core      domain + application + SQLAlchemy persistence (no crypto-library imports
                          in application; crypto confined to infrastructure)
packages/nervos-package-host  protocol only; no DB, no credentials, no network
apps/web          authenticated UI over the new APIs (TanStack Query, no localStorage)
```

- `nervos-core` gains `infrastructure/secrets/` (the only crypto-adapter location) and
  `infrastructure/sandbox/` (OS-primitive adapters). Application code depends on Protocols.
- The API never resolves a secret value; it manages metadata and encrypted rows only.
- The Worker resolves secrets only at dispatch time, inside the broker, and only for an allowed
  action; resolved values are used once and never persisted or logged.

## 6. Milestones and acceptance criteria

| Milestone | Scope | Acceptance (all must pass before "complete") |
|---|---|---|
| H0 | This plan + ADRs 0032–0036 | Documents exist; guard tests updated; no runtime change by H0 alone |
| H1 | Secret Manager: domain, AES-256-GCM envelope encryption, key config, migration `0016_stage_h1_secret_manager`, service, API, UI, tests | Round-trip, ciphertext-at-rest, wrong/missing key fail-closed, key versioning/rotation, disable/revoke, ownership isolation, no value in any response/log/error |
| H2 | Account connections + broker: migration `0017_stage_h2_account_connections`, connection lifecycle, server-side credential resolution | Owner/provider/scope binding, expiry/refresh/disconnect/revocation fail closed, tokens never at package/model/browser boundary |
| H3 | Per-action approval: migration `0018_stage_h3_action_approvals`, durable state machine, mediator gate, API, UI | Ask→pending→approve→one dispatch; deny/expire/cancel/revoke→zero dispatches; digest/schema/Run-snapshot/owner/Worker-restart/races fail closed; audit clean |
| H4 | Sandbox: OS containment per platform, resource/filesystem/network policy, migration `0019_stage_h4_sandbox_policy` (settings), platform tests | Path traversal/protected-file/child-process/limits/network attempts all blocked on supported platforms; unsupported platforms refuse to run; Worker authority proven |
| H5 | Trust + revocation: migration `0020_stage_h5_publisher_trust`, trust lifecycle, install/execution gates, closeout | Revoked publisher blocks install/rebind/new execution; installed/queued/historical behavior defined; full regression + migration + security gates pass |

Milestones are tested independently; each lands with its own focused tests, and the broad
repository gates run at closeout.

## 7. Platform support (frozen for H4)

| Platform | Isolation primitive | Status |
|---|---|---|
| Windows 10/11 | Job Object (`JOB_OBJECT_LIMIT_*`, UI restrictions) via pywin32-free ctypes | implemented + tested locally (Windows 11) |
| Linux 5.x+ | cgroup v2 + rlimits + `unshare` where available | implemented; CI acceptance pending where kernel features are unavailable |
| macOS | not supported for hostile code in Stage H | packages **refuse to execute** with a safe, explicit error |

Where adequate containment cannot be established, the Worker **fails closed** before dispatch
with a static error code; it never falls back to running the package unsandboxed.

## 8. Documentation and operational contract

- `.env.example` carries fake placeholder key configuration (`NERVOS_SECRETS_KEY_FILE`).
- Operator docs cover: key setup/rotation/recovery, secret lifecycle, connection lifecycle,
  approval semantics and expiry, sandbox platforms and limits, trust and revocation, and exactly
  which claims are tested.
- Loss of the encryption key renders stored secrets permanently undecryptable; recovery is
  re-entering values after re-keying. This is documented, not hidden.

## 9. Architecture-change protocol

Any conflict with ADRs 0015–0017 (Stage D tool authority), 0021–0023 (Stage F), 0024–0026
(Stage G), 0027–0030 (Stage I), or 0031 (runtime integration) requires
`STAGE H ARCHITECTURE CHANGE REQUEST — <issue>` with the conflict, affected ADRs, proposed
resolution, migration/compatibility impact, and required tests — before implementation.

## 10. Acceptance status and residual risk

All five milestones are delivered. The evidence and the limits of that evidence are both recorded
here rather than left to the reader's optimism.

### Verified locally on Windows 11

- The full architecture suite (89 tests), the Stage-H API route suite, the Stage-H core security
  integration suite, the approval-gate unit suite, and the sandbox suite pass.
- The **Windows Job Object** containment path is exercised directly: basic limits, UI restrictions,
  and the extended-limit class, using the correct `JOBOBJECTINFOCLASS` values.
- `ruff check`, `pyright`, the frontend typecheck, lint, and production build are clean.

### Not verified on this host — reported as pending, never as passing

- **Linux (cgroup v2 + rlimits)** containment is implemented but its tests **skip** on a Windows
  host. Linux acceptance is outstanding.
- **macOS** deliberately refuses to execute packages: `create_containment()` raises
  `ContainmentUnavailable` and the Worker fails closed before dispatch.

### Residual risks that Stage H does not remove

- An attacker running as the **same OS user** outside the sandbox is out of scope. Such code can
  read the database and the key file; no in-process defense defeats it.
- **OAuth authorization flows are not implemented.** Account connections manage grants, scopes, and
  brokering; obtaining a token by talking to a provider's authorization server is not delivered.
- Hosted Marketplace production acceptance, OIDC, and external publication authority remain
  **Stage I** work.
- Losing the Secret Manager key makes every stored secret permanently undecryptable. Recovery is
  re-entering values after re-keying.

## 11. Unresolved deviation from ADR 0034 (reported, not silently amended)

ADR 0034's **Audit** section states that approval decisions gain two new Run Event types,
`tool.approval_requested` and `tool.approval_decided`. **That was not implemented.** Neither literal
exists anywhere in `apps/` or `packages/`; `RunEventType` in
`packages/nervos-core/src/nervos_core/domain/jobs.py` was not extended.

What the implementation actually provides:

- the durable `action_approvals` row, which is the authoritative record of a request, its
  resolution, who decided, and when; and
- the **existing** `tool.denied` Run Event for every refusal.

The ADR's underlying intent — durable, public, credential-free evidence of an approval decision —
is therefore met, but not by the mechanism the ADR names. This is reported rather than rewritten
because ADR 0034 is accepted Stage-H authority and silently editing it is exactly what the
architecture-change protocol forbids.

```text
STAGE H ARCHITECTURE CHANGE REQUEST — ADR 0034 audit mechanism
```

Adding the two event types would require a new migration to widen the `run_events.event_type`
vocabulary, which would move the head past `0020_stage_h5_publisher_trust` and require the Worker,
Scheduler, architecture guard, and API migration tests to be updated together. That is a scope
change to a frozen milestone, so it needs explicit authorization before it is done.

## 12. Browser session race repaired at H5 closeout

Completing first-run setup could bounce the new administrator back to `/setup` and tear down the
dashboard a tick after it appeared. The cause was ordering, not Stage-H logic: `SetupPage` and
`LoginPage` navigated imperatively immediately after writing the session, so the router re-rendered
with the new location while the route gate still held the previous session and redirected back.

The fix makes the **gate own** the post-authentication transition. `SessionView` derives the
destination from the session it is rendering with, so a redirect is never computed from superseded
state. Two regression tests in `apps/web/src/App.test.tsx` assert the whole visited path sequence
rather than the final route, because the race always recovered before the final assertion could
see it.

## 13. Successor: pre-exec isolation and the qualified platform (2026-10-04)

ADR 0037 reopened the closeout above: resource primitives are not a sandbox. ADR 0038 replaced
the H4 design with a **pre-exec launcher** that establishes protection before package code
exists, and migration `0023_worker_sandbox_capability` moved sandbox capability reporting from
the API host to the Worker that actually executes packages.

### Qualified and refused platforms

| Platform | Backend | Status |
|---|---|---|
| Linux with `bwrap` installed | bubblewrap user/mount/PID/network namespaces, capability drop, cleared environment, frozen POSIX rlimits | **qualified** |
| Linux without `bwrap`, Windows, macOS | none | refused with `sandbox_unavailable` before any package import |

Windows Job Objects and plain POSIX rlimits remain resource primitives used by trusted tests.
They are never returned by the production factory and never report `FULL`.

### Qualification command and evidence

`make qualify-linux` (`scripts/linux_qualification.py`) is the repository-owned target. It proves
twelve properties and exits `0` qualified, `1` on failure, `2` on a prerequisite skip that is
never a pass: protected host file unreadable, direct outbound connection denied, scratch
writable, package environment read-only, sibling scratch/environment unavailable, descendants
killed on termination, resource and aggregate-output limits binding, a normal signed package
completing through package-host IPC, mediated model and granted MCP calls working, an ungranted
tool staying denied, structured context and selected memory reaching the SDK, and memory
proposals following manual/review/automatic-private policy without granting authority.

Recorded run: all twelve properties passed on Linux `6.18.40.1-microsoft-standard-WSL2` with
bubblewrap `0.8.0`, integrated phase `17 passed`, exit code `0`. That kernel is the Docker
Desktop WSL2 kernel, so this is **container/Linux-kernel qualification, not native-host
acceptance**; no native Linux NervOS host was available for this closeout. On Windows the
command reports the prerequisite skip (exit `2`), and the repository proves the refusal path
directly instead: `test_package_execution_refuses_before_any_spawn_on_unqualified_platforms`
asserts the static `sandbox_unavailable` outcome with no process started, and the installation
health check refuses before any package import.

### Interpreter prerequisite discovered during qualification

The launcher mounts read-only system roots only (`/usr`, `/usr/local`, `/opt`, `/lib`, `/lib64`,
`/bin`) plus the dynamic loader's own cache, and it never mounts the user home. A NervOS install
whose Python lives under a system root is qualified; one whose interpreter lives in the user home
cannot be exposed without mounting that home, so the host reports
`package_execution_supported: false` and package execution is refused. That refusal is correct:
the alternative is widening the mount surface, which ADR 0038 forbids. Point `uv`/`venv` at a
system location (for example `/opt`) on hosts that run package execution.

### Browser acceptance

`uv run python scripts/e2e.py` starts a supervised API, Worker and Web stack against an isolated
temporary database and package store and drives the deterministic browser journeys. On this
Windows host three journeys pass and the Stage-G package-install journey cannot: installing a
package now requires qualified isolation, so the install is refused and the dashboard shows
"Package containment is unavailable on this platform; execution is refused." That is the ADR
0037/0038 contract surfacing correctly, and it is not worked around with a configuration bypass.
The same supervisor run **passes end to end on the qualified Linux environment** (all four
journeys, exit code `0`), which is where package installation, execution and the browser journey
are accepted.

### ADR 0034 audit mechanism — resolved

Section 11 reported that `tool.approval_requested` and `tool.approval_decided` were never
implemented. Migration `0021_stage_h_approval_events` now extends the Run-event vocabulary with
both events, `RunEventType` carries them, the Run timeline renders them, and
`test_same_attempt_approval_resumption_and_refusals` asserts each is written. The reported
architecture-change request is closed by implementation; ADR 0034 itself is unchanged.

### Residual risk

An attacker running as the same OS user outside the sandbox can still read the database and the
key file; no in-process defense defeats it. Hosted Marketplace production acceptance, OIDC and
external publication authority remain Stage I work. Durable LangGraph/framework checkpoints and
universal SDK compatibility remain future work.
