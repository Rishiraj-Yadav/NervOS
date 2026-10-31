# ADR 0041 — Generic Process Agent Bridge

**Status: ACCEPTED** — Implemented per Owner decisions below.

## Context

NervOS today executes agents exclusively through the `nervos-sdk` Python entrypoint (`module.path:Symbol`) inside a sandboxed subprocess (`nervos_package_host`). The Worker mediates all model and tool access via a framed JSON wire protocol over stdin/stdout (G3 wire protocol v1).

Stage H4 qualifies package execution on Linux with bubblewrap (`--unshare-all`), providing filesystem and network namespace isolation (`packages/nervos-core/src/nervos_core/infrastructure/sandbox/linux_bubblewrap.py:79-80`). The sandboxed process communicates with the Worker solely via stdio pipes.

On Windows, the Job Object containment adapter reports `ContainmentTier.RESOURCE` (`packages/nervos-core/src/nervos_core/infrastructure/sandbox/windows.py:215`) which provides only resource limits (memory, CPU, process count) — **no filesystem write isolation and no network isolation** (`windows.py:10-12`). The Worker refuses package execution on Windows by default: `create_containment()` raises `ContainmentUnavailable` unless `NERVOS_ALLOW_WINDOWS_SANDBOX` is set to "1", "true", or "yes" (`packages/nervos-core/src/nervos_core/infrastructure/sandbox/__init__.py:42-49`). Even when enabled, the launcher probe returns `False` for Windows (`__init__.py:73-74`), so `observe_sandbox_capability()` reports `package_execution_supported=False`.

**Problem**: Developers want to run agents built on arbitrary SDKs (LangChain, LlamaIndex, Vercel AI SDK, custom Go/Rust/Node binaries) without rewriting them against `nervos-sdk`. The agent must remain sandboxed and reach models/tools **only through NervOS**.

## Decision

We will define a **Generic Process Agent Bridge** allowing any executable to run as a sandboxed NervOS agent, subject to:

1. **Transport**: Design A — The package host (already inside the sandbox) starts a loopback shim serving an OpenAI-compatible `/v1/chat/completions` endpoint and an MCP endpoint. The shim translates every request into the EXISTING `model_request` / `tool_request` frames over the EXISTING stdio pipe to the Worker. Grants, approvals, and audit stay in the Worker. The foreign agent runs as a child in the same sandbox, with base URL and per-run random API key supplied via environment.
   - `[unverified]` Whether 127.0.0.1 works inside the Linux bubblewrap network namespace; Linux-only test added (skipped on non-Linux). See docs/linux-verification-runbook.md — still unverified until an owner run.

2. **Manifest**: Extend `runtime` section with optional `kind` (values: `python` [default], `process`). For `process`, require `runtime.command`: a non-empty list of strings (first item a relative path inside the package bundle). Validate: no absolute paths, no `..`, no shell strings, max 32 items, each max 512 chars. `runtime.entrypoint` and `runtime.python` stay required only for `python` kind. Files: `packages/nervos-core/src/nervos_core/application/package_manifest.py` and `packages/nervos-core/src/nervos_core/domain/packages.py`.

3. **Model Access**: Worker exposes provider-neutral model mediation via wire protocol `model_request`/`model_response` (existing). The loopback shim in the package host translates OpenAI/Anthropic-compatible HTTP requests to these frames.

4. **Tool Access**: Worker exposes granted tools via wire protocol `tool_request`/`tool_response` (existing, handled at `apps/worker/src/nervos_worker/package_execution.py:314`). The loopback shim exposes an MCP endpoint that translates to these frames. Permission checks, approvals, audit remain in Worker (`ToolInvocationMediator`).

5. **Credentials**: **No real provider keys enter sandbox**. `package_host_environment()` allowlists only SYSTEMROOT, WINDIR, COMSPEC, PATHEXT, TEMP, TMP, TMPDIR, HOME, PATH, LANG, LC_ALL (`apps/worker/src/nervos_worker/package_execution.py:759-775`). Worker holds credentials; sandbox sees only per-run random API key. Worker injects real keys when calling provider adapters.

6. **Containment**: Full bubblewrap containment (`--unshare-all`) applies on Linux. Windows/macOS do not provide FULL tier.

7. **Test Requirement**: A "foreign agent" (no `nervos-sdk` import) using an unmodified OpenAI-compatible or MCP client must: (a) complete a Run, (b) fail on direct network call outside NervOS, (c) be denied ungranted tool with audit, (d) leave durable audit records.

### Platform Guarantees

| Platform | Tier | Enforces "only through NervOS" | Product Behavior |
|----------|------|-------------------------------|------------------|
| Linux (bubblewrap) | FULL | Yes — `--unshare-all` isolates network and filesystem | Allow execution |
| Windows (Job Object) | RESOURCE | No — no network isolation, partial filesystem isolation | Refuse by default (existing behavior unchanged) |
| macOS / other | UNSUPPORTED | N/A | Refuse |

### Owner Decisions

1. **Windows behavior unchanged**: Package execution stays refused unless the existing env flag `NERVOS_ALLOW_WINDOWS_SANDBOX` allows it; do not weaken any containment.
2. **Streaming deferred**: Not implemented in this milestone.
3. **Images/multimodal deferred**: Not implemented in this milestone.
4. **Per-run random API key**: Replaces the fixed "nervos-proxy" placeholder; a new random token is generated per Run.
5. **No live Linux sandbox verification**: The loopback assumption stays `[unverified]`; Linux-only test is skipped with reason "requires bubblewrap; loopback in namespace unverified".

## Consequences

### Positive
- Language-agnostic agent execution without SDK rewrite
- On Linux FULL tier: preserves Stage H4 isolation (no network, no host filesystem, resource limits)
- Reuses existing Worker authority: model routing, tool grants, audit, retry, cancellation
- No new execution path — generic agent runs inside same `PackageExecutionAdapter` seam
- Credential isolation by construction: sandbox never sees real keys

### Negative
- **Loopback HTTP inside sandbox is [unverified]** on Linux; if 127.0.0.1 does not work, Design A fails and stdio wire is the only transport
- **Streaming not supported** in current wire protocol/types (requires v2 + SDK changes)
- **No multimodal/images** — `ModelMessage.content: str` only (`packages/nervos-sdk/src/nervos_sdk/types.py:54`)
- **Manifest breaking change** — existing packages require `language: python`; migration needed
- **Windows does not enforce isolation** — product refuses by default

### Risks
- Design A depends on loopback working in bubblewrap namespace; Linux-only test is a gate
- Wire protocol v2 design must not break existing package-host compatibility
- Capability negotiation (`host_hello` features) must express streaming, tool modes, MCP
- Non-Python agents need reference wire client implementations (Python, Node, Go, Rust)
- Resource limits (rlimits) tuned for Python; may need per-language profiles

## Alternatives Considered

| Alternative | Rejected Because |
|-------------|------------------|
| Stdio-only wire protocol (no loopback shim) | Requires every foreign SDK to implement custom wire client; Design A allows unmodified OpenAI/MCP clients |
| Loopback HTTP without shim (Worker serves HTTP) | Worker would need to expose HTTP inside sandbox; shim keeps HTTP in package host, translates to existing wire frames |
| VSOCK / Unix domain sockets | Requires `--share-net` or host bind mounts; weakens isolation |
| Agent imports `nervos-sdk` as library | Defeats language-agnostic goal; couples to Python SDK |
| Separate "generic worker" process | Violates modular monolith; duplicates Stage C/D authority |
| MCP-over-stdio for tools only | Wire protocol already has `tool_request`/`tool_response`; shim translates MCP to existing frames |

## Implementation Plan (Milestones)

See `docs/generic-agent-bridge/plan.md` for detailed milestones M0–M7.

**M0 (this ADR)**: Architecture freeze, six design questions answered with evidence.
**M1**: Manifest extensions (`runtime.kind`, `runtime.command`), wire protocol capability flags.
**M2**: Loopback shim in package host (OpenAI-compatible `/v1/chat/completions`, MCP endpoint) + Linux-only test.
**M3**: Tool shim (MCP → `tool_request`/`tool_response`) + permission enforcement.
**M4**: Credential isolation + per-run random API keys.
**M5**: Test suite (shim unit tests, e2e test with fake foreign agent, Linux-only test).
**M6**: Documentation + examples.

## Verification

- `git hash-object docs/adr/0041-generic-process-agent-bridge.md` reported at M0 review
- All six design questions answered with file/line evidence in plan.md
- No implementation before external review accepts this ADR
- Conflict with Stage H4 (`--unshare-all` network isolation) or Stage G (manifest schema) is a stop condition
- **M2 gate**: Linux CI test must verify 127.0.0.1 works inside bubblewrap namespace before Design A is accepted

## References

- `docs/generic-agent-bridge/plan.md` — Milestones, acceptance criteria, evidence index
- `packages/nervos-core/src/nervos_core/infrastructure/sandbox/linux_bubblewrap.py:79-80` — `--unshare-all`
- `packages/nervos-core/src/nervos_core/infrastructure/sandbox/windows.py:10-12,215` — Windows RESOURCE tier, no network isolation
- `packages/nervos-core/src/nervos_core/infrastructure/sandbox/__init__.py:42-49,73-74` — Windows default refusal, probe returns False
- `packages/nervos-package-host/src/nervos_package_host/wire.py` — Wire protocol v1
- `packages/nervos-sdk/src/nervos_sdk/ports.py` — ModelPort/ToolPort
- `apps/worker/src/nervos_worker/package_execution.py:314,759-775` — tool_request handling, package_host_environment allowlist
- `packages/nervos-core/src/nervos_core/application/package_manifest.py:261-267` — Manifest runtime parsing
- `packages/nervos-core/src/nervos_core/domain/packages.py:36-52` — Entrypoint validation
- `docs/stage-h/README.md` — Stage H4 containment qualification