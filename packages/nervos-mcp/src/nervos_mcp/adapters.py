"""Bindings from this package's concrete configuration to ``nervos-core``'s provider-neutral ports.

``nervos-core`` defines the shapes it needs -- how to discover a catalog, and which operator facts a
connection may be validated against -- without knowing that an MCP SDK exists. This module is the
other half of that seam, and it is the only place the two are joined.

Both bindings are deliberately *predicates* rather than throwing operations. The service asks
"would the operator allow this?", and a refusal is an ordinary ``False`` there; turning it into a
message is the service's job, because it is the layer that knows which durable error a refusal
becomes. That also keeps a remote-controlled string from ever travelling through a boolean.
"""

from __future__ import annotations

from nervos_core.application.mcp_connection_service import McpDiscovery
from nervos_core.application.mcp_connections import McpConnectionRow
from nervos_core.application.tool_registry import ToolDefinitionMaterial

from nervos_mcp.discovery import discover_tools
from nervos_mcp.errors import McpError
from nervos_mcp.operator_config import McpOperatorConfig
from nervos_mcp.policy.egress import EgressPolicy


class OperatorFacts:
    """Answer the service's operator questions from declared configuration alone.

    ``endpoint_allowed`` runs the same strict egress policy the transport will run again moments
    before a socket exists. Deciding here is what makes "the operator never allowlisted this origin"
    a configuration error the owner sees at create time, rather than a discovery failure they have
    to interpret later.
    """

    def __init__(self, config: McpOperatorConfig, policy: EgressPolicy) -> None:
        self._config = config
        self._policy = policy

    def check_alias_target(self, alias: str, target: str) -> bool:
        try:
            self._config.check_alias_target(alias, target)
        except McpError:
            return False
        return True

    def stdio_server(self, server_key: str) -> bool:
        return server_key in self._config.stdio_servers

    def endpoint_allowed(self, endpoint: str) -> bool:
        try:
            self._policy.validate(endpoint)
        except McpError:
            return False
        return True


def build_discovery(config: McpOperatorConfig, policy: EgressPolicy) -> McpDiscovery:
    """Return the provider-neutral discovery callable the connection service invokes."""

    async def discover(connection: McpConnectionRow) -> tuple[ToolDefinitionMaterial, ...]:
        return await discover_tools(connection, operator=config, policy=policy)

    return discover


__all__ = ["OperatorFacts", "build_discovery"]
