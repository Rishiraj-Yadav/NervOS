"""NervOS MCP client, gateway, and connection lifecycle.

This package is the only place in the repository that imports the official MCP SDK. Everything a
provider-neutral caller needs is expressed as something from :mod:`nervos_core`: a
:class:`~nervos_core.domain.tools.ToolDescriptor`, a
:class:`~nervos_core.application.tool_registry.ToolResult`, or a classified failure. No SDK object
crosses this boundary, and this package never depends on ``nervos-models``.
"""
