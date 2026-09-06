---
name: add-mcp-tool
description: Add or integrate a NervOS MCP/tool capability through the tool registry and permission layer, with schemas, timeouts, credential isolation, audit events, and tests. Intended for Stage D and later.
---

# Add an MCP/Tool Capability

Do not implement MCP during Stage A.

Read `docs/architecture.md`, `docs/permissions.md`, and security rules.

## Define

- stable granular capability name
- input schema
- output schema
- side-effect/risk class
- credential requirement
- approval policy
- timeout/retry behavior
- idempotency expectations

Prefer `gmail.send` over a broad `gmail` permission.

## Security invariant

Tool credentials stay in NervOS secret/tool-connection storage.

Flow:

`Agent -> Tool API -> Permission Engine -> MCP Gateway -> Tool`

Agent code receives capability results, not raw credentials.

## Tests

Cover valid invocation, schema failure, permission denial, approval requirement where applicable, timeout/error mapping, missing credentials, and audit event creation.
