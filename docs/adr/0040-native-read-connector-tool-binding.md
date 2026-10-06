# ADR 0040 — Native read connector tool binding

Status: Accepted for implementation under the owner's W0–W6 integration repair
authorization, 2026-10-04. Implemented and qualified on 2026-10-05;
see the autonomous-workflow verification closeout. Live Google account testing remains
a deployment prerequisite.

Gmail list/get are native brokered connectors, not an MCP server. Register their
exact schemas as built-in durable definitions and allow packages to bind their
declared aliases to these two definitions through the existing integration
binding service. The legacy `mcp_tools` manifest field remains additive: exact
name/schema/fingerprint checks apply to these native read definitions too.

No general widening to arbitrary built-ins is permitted. Ordinary MCP bindings
keep connection ownership checks. Native reads require an explicit per-instance
grant and an operator binding to an owner-connected account, with an exact
readonly scope and fixed Gmail origin. The Worker broker retains credentials,
checks current authority, refreshes tokens, and dispatches through the existing
mediator and audit ledger. Missing bindings fail closed.

There is no native sending, draft mutation or deletion operation. Decision demos
may propose another read/classification step; displaying a reply draft does not
make sending available. Installation and account connection grant no permission.
