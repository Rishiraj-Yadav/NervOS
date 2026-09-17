# ADR 0016 — MCP Client, Discovery, and Connection Trust Boundary

Status: Proposed — D0 architecture freeze, pending external review

This ADR freezes how NervOS speaks MCP, what it will and will not connect to, and where the boundary
between NervOS policy and the official protocol implementation sits. Authority and audit are
ADR 0015; durability is ADR 0017.

## Context

Stage D is the first time NervOS connects to anything outside itself that it does not control. Three
constraints shaped every decision below.

**The protocol moved.** MCP `2026-07-28` is the current revision, and it is **stateless**: there is
no `initialize` handshake, no session, and — decisively — **servers may no longer initiate JSON-RPC
requests**. Protocol version travels on every request in `_meta`. A client written against the
handshake era is not merely out of date; it is modelling a different program.

**The official SDK is real and current.** MCP Python SDK v2 went GA as 2.0.0 on the same date as the
spec revision, is at 2.2.0, and implements the modern protocol with first-class transport injection.
Writing a second protocol implementation would mean owning JSON-RPC framing, version negotiation,
SSE handling, and error mapping forever, for no capability NervOS actually needs.

**Two surfaces would be remote code execution and SSRF if exposed naively.** A stdio MCP server is a
subprocess running with the Worker's operating-system privileges. A user-supplied HTTP URL is a
request from inside the operator's network to wherever that URL names. Stage H owns the real
sandboxing and network isolation; Stage D must not pretend those problems are absent, and must not
wait for Stage H to become usable.

## Decision

### The official SDK is the protocol implementation

Stage D uses the **official MCP Python SDK v2**. NervOS does **not** implement its own JSON-RPC or
MCP protocol stack.

The reason is a division of ownership that should be obvious in hindsight:

**NervOS owns policy** — permissions, catalog filtering, transport trust restrictions, connection
configuration, credential references, timeouts, bounded teardown, audit, and execution authority.

**The SDK owns the protocol** — message framing, `server/discover`, `tools/list` and `tools/call`
models, protocol-version handling, and MCP error mapping.

NervOS-owned wrappers around the SDK's transports are permitted where policy must be injected:
HTTP egress validation, redirect refusal, timeouts, bounded lifecycle, and stdio operator
configuration. NervOS does **not** re-implement protocol semantics inside those wrappers.

**Intended dependency range — frozen now: `mcp >= 2.2, < 3`.**

The floor is the current stable v2 line as researched at D0 (`2.2.0`, released 2026-09-07; the v2 line
became GA at `2.0.0` on 2026-07-28). Freezing it here means D1's schema is designed against a known
protocol implementation rather than a range whose floor was still open.

**Python compatibility is satisfied.** The SDK requires `>= 3.10`; the workspace requires `>= 3.12`.
No incompatibility. If evidence had shown the floor invalid, this freeze would have stopped as
**`D0 REVISION BLOCKED — MCP SDK VERSION RANGE INVALID`** rather than been adjusted quietly.

**Minor and patch updates inside that range are permitted only while the accepted protocol and API
assumptions continue to hold** — specifically: that `2026-07-28` remains the negotiation target, that
`server/discover`, `tools/list` and `tools/call` keep their verified shapes, that transport injection
remains available, and that no new default begins caching or auto-executing anything. A v2 minor that
breaks any of those assumptions is a **reviewed change**, not a routine bump.

**D0 adds nothing.** The dependency is declared at **D5**, where `packages/nervos-mcp` becomes a
workspace member and `pyproject.toml` and `uv.lock` are updated together. D0 records the contract only.

**If D0 or D5 discovers that the official SDK genuinely cannot implement this ADR**, the cycle stops:

> **D0 BLOCKED — OFFICIAL MCP SDK INCOMPATIBLE**

with the exact evidence, and a custom protocol client is considered only after external review. It is
not a fallback to reach for locally.

### Session and version behaviour

- **Target revision: `2026-07-28`.** Stage D declares the modern era explicitly.
- **There is no silent fallback to the legacy era.** A handshake-era peer receives a **clear,
  actionable unsupported-protocol error** naming it as a legacy server. The SDK's `auto` mode — one
  `server/discover` probe, then a fallback to `initialize` — is deliberately **not** used: it would
  double the connection state machine and reintroduce server-initiated requests that this revision
  removed.
- `UnsupportedProtocolVersionError` is a **handled path**, not an unexpected failure: it carries the
  server's supported versions, and the client either selects a mutually supported one or surfaces an
  error naming both sides.

**This is a deliberate narrowing.** A legacy-only server is unusable in Stage D, and that is stated
rather than worked around.

### Transports, and the one that is refused

**Supported: Streamable HTTP and stdio.** These are the only two standard bindings in `2026-07-28`.

**Legacy HTTP+SSE is not built against.** It has been deprecated since `2025-03-26` (SEP-2596),
though not removed; building a new integration on a deprecated transport would create work already
scheduled for deletion. A server offering only legacy SSE is unsupported in Stage D.

The SDK's `sse_client` remains importable and is explicitly **not used**.

### Extensions: none in Stage D

Tasks, Apps, and Enterprise-Managed Authorization are all Stable in `2026-07-28`. All three are
**deferred**, and in every case the reason is architectural rather than a maturity concern:

- **Tasks** would add a second asynchronous lifecycle — polling `tasks/get` — beside the durable
  Job/Attempt lifecycle that Stage C established as the *only* execution authority. Two schedulers
  for one action is precisely what the accepted architecture forbids. Plain `tools/call` satisfies
  every Stage D requirement.
- **Apps** and **Enterprise-Managed Authorization** are not Stage D roadmap outcomes; the latter
  belongs with Stage H's credential architecture.

**No extension is advertised in `clientCapabilities` unless it is implemented.** Resources and
Prompts are likewise deferred: Stage D's roadmap line is the tool registry.

### Credentials: an opaque operator-declared alias, never an environment variable name

A credential reference that simply named an environment variable would let a user write
`credential_ref = OPENAI_API_KEY` and cause the Worker's model-provider secret to be forwarded to an
arbitrary MCP server. That is a token-passthrough mechanism built by accident.

**Frozen: `mcp_connections.credential_ref` is an opaque, operator-declared credential alias** — for
example `github-prod`. It is **not** an environment variable name, and a connection may only
reference an alias the operator has made available to that connection.

Operator-owned configuration maps each alias:

```
alias  →  environment variable name
       →  permitted target(s):  HTTP origin(s), or stdio server key(s)
       →  supported auth scheme
```

- The **browser and the API may only select an alias offered for that connection.** The environment
  variable name is never user-controlled and is never returned by any endpoint.
- **Stage D supports one auth scheme: a bearer token** read from the alias's variable at call time.
  There are **no arbitrary user-defined headers**, because a header name chosen by a user is a
  credential-exfiltration surface.
- **No OAuth flow exists in Stage D.** The `2026-07-28` authorization requirements — RFC 9728
  protected-resource metadata, the RFC 8707 `resource` parameter, mandatory PKCE S256, audience
  validation, and the explicit prohibition on token passthrough — are demanding, and a half-built
  version would put a bearer token in NervOS with no encrypted store to keep it in. Shipping none of
  it means **there is no token in NervOS at all**. Authenticated remote servers require operator
  provisioning; Stage H replaces this with a real secret manager.

**Audience binding.** An alias is bound to its intended target. The runtime **refuses to attach an
alias to a connection outside its permitted set** — a different origin, or a different stdio server
key. Without this, one Worker environment credential would become a cross-service passthrough
mechanism the moment an operator reused an alias.

The raw secret value is never stored in SQLite, never returned by any API, never sent to a model,
never logged, and never placed in a Run, Job, Attempt, Run Event, or ToolInvocation.

### The stdio trust boundary

An arbitrary user-supplied command string is arbitrary code execution with the Worker's privileges.
It is not equivalent to adding an API URL, and Stage D does not treat it as one.

**Operator configuration declares each stdio server**: a `server_key`, the executable, its arguments,
its working directory, and the credential alias it may use. A user connection references the
**server key only**.

- There is **no API field and no UI control** that accepts a command, an argument, or a working
  directory, and no migration adds one.
- **No `shell=True`.** The executable and arguments are passed as a vector, never through a shell.
- **The child environment is built explicitly and minimally** from what the operator declared. The
  Worker's full environment is **not** inherited by default, so a compromised server cannot read
  unrelated secrets from the Worker's process.
- **Credential material is inserted only when the operator's configuration requires it for that key.**
- Teardown is **terminate, then kill after a bounded grace**, never an unbounded await. This follows
  the Worker's existing rule that a non-cooperative coroutine is abandoned with its outcome discarded
  rather than awaited forever.

**Documented limitation:** the official SDK's stdio parameters expose no process-group control, so a
wedged child cannot be reliably killed as a group. Bounded teardown, sequential process-heavy test
runs, and operator visibility are the mitigation; Stage H's sandbox is the real fix.

### The HTTP trust boundary

`NERVOS_MCP_ALLOWED_ORIGINS` is **operator-owned configuration**.

- **Empty means deny all remote HTTP MCP connections.** A fresh install can reach nothing.
- A user may create a connection **only to an origin the operator has permitted**.
- **HTTPS is required** except for an explicitly approved loopback or test exception.
- **Private ranges, link-local addresses, and cloud metadata endpoints are refused** under the
  reviewed policy, regardless of the allowlist.
- **Cross-origin redirects are refused**, not followed.
- **The policy is re-checked at dispatch time**, not only at save time, so a policy tightened after a
  connection was saved takes effect.

**Documented residual limitation:** this is an onboarding control, not a complete egress firewall.
Stage D does **not** claim to provide isolation from DNS rebinding, from a permitted origin that
itself proxies inward, or from any other network path. **Stage H owns network isolation**, and the
roadmap amendment records that explicitly.

### Refusals are disclosed, and they are a frozen contract

A limitation the user cannot see is indistinguishable from a bug. An earlier draft left disclosure as
a documentation and UI obligation; **the refusal behaviour is frozen here as a contract** so that the
limits are visible by behaviour rather than only by prose.

Every refusal below returns a **safe, stable error code with a static message** through the one error
envelope — never a stack trace, never the operator's configuration:

| Situation | Where | Code | What the message may say |
|---|---|---|---|
| Origin not in the operator allowlist | save time **and** dispatch time | `mcp_origin_not_permitted` | that the origin is not permitted for this server — **never which origins are** |
| Credential alias not offered for this target | save time **and** at the `started` re-check | `mcp_credential_alias_not_available` | that the alias is not available for this connection — never the variable name, never any value |
| Transport unsupported (e.g. legacy SSE) | save time | `mcp_transport_unsupported` | that the server's transport is not supported |
| Protocol era unsupported | first contact | `mcp_protocol_unsupported` | that the server is a legacy-era peer, naming both sides' revisions |
| Non-https origin off loopback, or a refused address range | save time | `mcp_origin_refused` | that the address is refused, without confirming what the range contains |

**The allowlist's contents are never enumerated in a response.** Telling a user "the permitted origins
are X, Y and Z" would turn an onboarding error into a disclosure of the operator's network policy, so
the code says a rule was applied and not what the rule contains.

Denials at the connection boundary are therefore **behaviourally pinned**, not merely described — the
same standard ADR 0015 applies to an unsupported tool schema, which is listed with its reason shown
rather than silently omitted.

### Discovery, caching, and the catalog

`tools/list` runs **once when a connection is saved**, again on an **explicit user refresh**, and
again on **reconnect** after a transport failure. It does **not** run per model turn.

- **The SDK's built-in response cache is disabled for discovery** (or every call is made in bypass
  mode). Cached metadata is never allowed to stand between the fingerprint check and the server's
  actual definition, because a stale catalog is exactly how a superseded capability would keep
  working.
- A server's `ttlMs` / `cacheScope` hints are consumed **to invalidate, never to extend**. In
  `2026-07-28` both fields are required on `tools/list` results and an absent `ttlMs` is read as `0`,
  so treating every catalog as immediately stale is always within the specification — which is what
  "the cache is never authority" does.
- **A cached descriptor whose fingerprint no longer matches fails closed** (ADR 0015).
- Server unavailable → the connection is marked `Unavailable` with a safe error and its tools are not
  offered; existing grants are untouched.
- Tool removed → definition retained, marked unavailable, grant suspended.
- Tool renamed → remove plus add: a new durable identity with no inherited grant.

**Connection definition and live client are different things.** The definition is the durable row.
The live client is ephemeral, owned by the Worker, created on first use, and closed on shutdown or
idle timeout. It is never a durable fact, and no correctness property depends on it surviving.

Disable and hard-delete semantics are frozen in ADR 0015.

### Architecture: where the SDK may live

```
nervos-core              protocol-neutral ports and domain only
packages/nervos-mcp      the official SDK dependency + the MCP adapter
composition roots        the only modules that wire the concrete package
```

**No `mcp` import may appear in** `nervos-core`, ordinary API route modules, ordinary Worker modules,
or the frontend. Only a composition root may reach the concrete package — the same rule
`test_provider_sdk_exists_only_in_the_models_infrastructure_package` already enforces for
`anthropic` and `openai`, extended to name this package.

Activating `packages/nervos-mcp` (currently a one-comment placeholder outside the uv workspace) is a
D5 change: it becomes a workspace member, enters pyright's `include`/`extraPaths` and pytest's
`testpaths`, and the dependency and `uv.lock` are updated there. **D0 changes none of that.**

## Consequences

The isolated SDK means a protocol change is absorbed in one package, and the loop, the permission
evaluator, and the audit never learn MCP has a version.

Choosing the SDK means accepting 14 transitive runtime dependencies, including an ASGI server stack
and `cryptography`, inside a self-hosted monolith — and accepting its current Windows stdio defects.
That is the price of not owning a protocol implementation, and it is paid deliberately rather than by
accident. Bounded teardown and sequential process-heavy runs are the mitigation for the stdio issues.

Supporting only the modern era and only two transports means some servers are unusable in Stage D.
That is stated in the runtime documentation and is reversible later by adding an era, not by
rewriting the loop.
