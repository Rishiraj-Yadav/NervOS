"""Worker-side MCP composition: which durable connections this process can execute against.

Two jobs, and neither of them is authority.

**Synchronisation** keeps the local :class:`~nervos_core.application.tool_registry.ToolRegistry` in
step with durable connection state. It exists because a Worker cannot assume it was the process that
performed a discovery: connection 12 may have been refreshed by the API, or by a different Worker,
and this process would otherwise never learn that its tools are now offerable. So before a
tool-enabled Attempt assembles its catalog, every connection that could contribute is registered
locally. This performs **no MCP network I/O** -- it constructs cheap adapters over durable rows.

Registering is not authorising, and a *missing* registration is not a denial either: D2's live
evaluator reads durable connection and definition state immediately before dispatch, so it is the
only thing that decides. That is why no invalidation message has to travel between Workers when one
disables a connection, and why a stale local registration is harmless -- the next permission check
denies the call from durable truth regardless of what this process has cached.

**Session lifecycle** is the gateway's: one process-local cache of live clients, keyed by connection
id, with lazy idle expiry and a bounded close at shutdown.
"""

from __future__ import annotations

from nervos_core.application.mcp_connections import McpConnectionPersistence
from nervos_core.application.tool_registry import ToolDefinitionPersistence, ToolRegistry
from nervos_core.domain.runs import Run
from nervos_core.domain.tools import ToolSourceKind, ToolSourceRef
from nervos_mcp.executor import McpToolExecutor
from nervos_mcp.gateway import McpGateway
from nervos_mcp.operator_config import McpOperatorConfig
from nervos_mcp.policy.egress import EgressPolicy
from nervos_mcp.source import McpToolSource


class McpRegistrySynchronizer:
    """Registers one local MCP source/executor pair per durable connection that can contribute."""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        connections: McpConnectionPersistence,
        definitions: ToolDefinitionPersistence,
        gateway: McpGateway,
    ) -> None:
        self._registry = registry
        self._connections = connections
        self._definitions = definitions
        self._gateway = gateway

    def synchronize(self) -> int:
        """Ensure every contributing connection has a local registration. Returns how many were new.

        Registration is idempotent and cheap, so this is safe to call before every tool-enabled
        Attempt. A connection that has been disabled or deleted since it was registered is simply
        left registered: removing it would be a local cleanup, and nothing depends on that having
        happened.
        """
        registered = 0
        known = set(self._registry.source_refs())
        for connection_id in self._connections.list_all_contributing_ids():
            source_ref = ToolSourceRef(ToolSourceKind.MCP, connection_id)
            if source_ref in known:
                continue
            self._registry.register(
                source_ref=source_ref,
                source=McpToolSource(
                    connection_id=connection_id, definitions=self._definitions                ),
                executor=McpToolExecutor(
                    gateway=self._gateway, connection_id=connection_id
                ),
            )
            registered += 1
        return registered

    async def synchronize_for_run(self, run: Run) -> None:
        """Make every connection this Run could draw on available in this process's registry.

        This is the seam D4 calls once per Attempt, immediately before it assembles the catalog, and
        it exists because a registry built once at startup is a snapshot: a connection the control
        plane created and refreshed a moment ago would otherwise stay invisible until the Worker was
        restarted, and a Run would silently lose a tool its own grant authorises.

        Three properties matter, and all three are about what this *cannot* do:

        * **It reads durable rows only.** No MCP client is opened, no ``tools/list`` is issued, no
          socket exists. Building a source and an executor is constructing two objects over
          persistence; the live session remains lazy inside the gateway, created only when a call
          actually dispatches.
        * **It is not scoped to the Run's owner.** Registering a source makes descriptors
          *gatherable*, and D2's live evaluator filters every one of them against the Run's own
          Agent Instance and the connection's owner, so a registration this Run may not use is
          dropped at assembly. Scoping here would be a second, weaker copy of a decision D2 already
          makes from authority.
        * **Registering is not authorising.** A disabled, drifted or foreign connection's tools are
          still denied at the first permission check, so a stale or over-broad registration can only
          ever withhold a tool, never permit one.
        """
        del run  # The registry is owner-agnostic by design; D2 decides what this Run may use.
        self.synchronize()

    def forget(self, connection_id: int) -> None:
        """Drop one local registration. Local cleanup only; correctness never rests on it."""
        self._registry.unregister(ToolSourceRef(ToolSourceKind.MCP, connection_id))


def build_mcp_gateway(
    *,
    connections: McpConnectionPersistence,
    operator: McpOperatorConfig,
    policy: EgressPolicy,
) -> McpGateway:
    """Build the Worker's live-client cache.

    The strict policy is passed in rather than constructed here, so the only place that can choose a
    policy is the composition root -- which is also the only place that could choose a permissive
    one, and does not.
    """
    return McpGateway(connections=connections, operator=operator, policy=policy)


__all__ = ["McpRegistrySynchronizer", "build_mcp_gateway"]
