"""``McpToolSource``: the durable catalog of one MCP connection, as D4 needs to see it.

A source answers exactly one question -- *which tools does this connection currently offer?* -- and
it answers it from **persisted, reconciled definitions only**. It never calls ``tools/list``.

That is a deliberate restriction rather than an optimization. D4 freezes one catalog per Attempt,
and a source that reached the network during assembly would make that frozen catalog depend on when
the
assembly happened and on whether a remote server was reachable -- so a Run could offer a different
tool set on retry, and an "immutable catalog" would be immutable in name only. Discovery is a
lifecycle operation with its own transaction and its own deadline; this is a read of its result.

Only ``available`` definitions are returned. A definition marked ``unsupported_schema`` is stored,
fingerprinted and auditable, but it is never offered -- D3's rule that a tool whose contract NervOS
cannot understand is not exposed.
"""

from __future__ import annotations

from collections.abc import Sequence

from nervos_core.application.tool_registry import ToolDefinitionPersistence
from nervos_core.domain.tools import (
    DefinitionStatus,
    ToolDescriptor,
    ToolSourceKind,
    ToolSourceRef,
)


class McpToolSource:
    """The durable tool catalog of one MCP connection."""

    def __init__(self, *, connection_id: int, definitions: ToolDefinitionPersistence) -> None:
        self._connection_id = connection_id
        self._definitions = definitions
        self._source_ref = ToolSourceRef(ToolSourceKind.MCP, connection_id)

    async def list_tools(self) -> Sequence[ToolDescriptor]:
        """Return the connection's available definitions as provider-neutral descriptors.

        Each descriptor carries its durable id, which is what a grant and an invocation record refer
        to; no SDK object is reachable from here, and none is constructed.
        """
        return tuple(
            definition.to_descriptor()
            for definition in self._definitions.list_for_source(source_ref=self._source_ref)
            if definition.material.status is DefinitionStatus.AVAILABLE
        )


__all__ = ["McpToolSource"]
