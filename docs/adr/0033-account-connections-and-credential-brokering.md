# ADR 0033 — Account Connections and Credential Brokering

Status: Accepted for Stage H implementation (H2)

## Context

ADR 0016 froze the Stage-D credential story: operator-declared aliases, bearer tokens from the
Worker environment, **no OAuth anywhere** — "Stage H replaces this with a real secret manager."
An MCP server connection (where tools come from) is not the same thing as a user's account
connection (whose authority acts). Conflating them is how "Gmail access" becomes one giant
permission. The Gmail/email example must not become a hardcoded connector; the interface has to
suit multiple providers.

OAuth in scope: **yes, minimally** — the authorization-code flow with PKCE S256 for HTTP
providers, executed by the local API against a provider the operator has configured. Device
flow, client-credentials service accounts, and arbitrary header schemes are out of scope for H2.

## Decision

### Two distinct records

- `mcp_connections` (Stage D) — where a tool comes from. Unchanged authority.
- `account_connections` (H2, new) — a user's connection to an external account provider, bound
  to: owner, provider id, granted scopes, lifecycle state, expiry/refresh metadata, and a
  reference into the Secret Manager holding its tokens.

### Token custody

Access and refresh tokens live **only** in the Secret Manager (encrypted, ADR 0032). They are
never returned by any API, never rendered in the browser (the UI shows status, scopes, expiry),
never sent to packages, never placed in model prompts or Run events.

### The connector broker

All authorized account actions route through a NervOS-owned **connector gateway**:

```text
package/model intent → Stage-D permission check (unchanged)
    → H3 approval (if the action requires it)
    → broker: resolve credential server-side, re-check owner/agent/connection/scope/permission
    → dispatch with credential attached inside the NervOS process
```

The broker is a core application service; its concrete HTTP transport is composed at the Worker
boundary. At invocation time it re-checks, fail-closed, in order:

1. the connection exists, is owned by the Run's owner, and is `connected`;
2. the connection's granted scopes include the action's required scope;
3. the credential resolves (secret exists, is enabled, decrypts under the current key);
4. the token is unexpired, or a refresh succeeds within the broker's bounded window;
5. the Stage-D permission decision for this agent/tool is `allowed` (never assumed from the
   connection existing).

Any failure denies the action with a safe, static error; nothing is dispatched.

### Refresh, expiry, and disconnect

- Refresh happens only inside the broker's dispatch window, with bounded retries, never in a
  database transaction; a failed refresh marks the connection `needs_refresh` and fails the
  action closed.
- Expired connections deny actions; they do not silently refresh on a timer.
- Owner disconnect revokes local authority immediately (next-check semantics) and attempts a
  best-effort provider revocation, recording the outcome without blocking the disconnect.
- Provider-side revocation surfaces as a failed refresh/action and a `needs_refresh` or
  `disconnected` state — never as an assumed-valid token.

### What stays out

- No Gmail-specific shortcut, no per-provider branches in core; providers are configuration
  (operator-declared: id, authorization/token endpoints, scopes) and one generic connector
  transport.
- No token passthrough: a package can no more name a connection's token than Stage D's
  connection could name an environment variable (ADR 0016's exact lesson).
- No hosted Marketplace interaction; this is local runtime security only.

## Consequences

- "Tool connection ≠ agent permission" (permissions doc) is now enforced twice: once by Stage-D
  grants, once by connection scope at dispatch.
- A compromised package gains nothing credential-shaped: it can only request mediated actions.
- The operator must configure OAuth providers explicitly (client id/secret in Worker
  environment, redirect URIs to the local API) before any account connection can exist — the
  same fail-closed posture as Stage D's empty origin allowlist.
