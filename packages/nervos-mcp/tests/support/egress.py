"""A **test-only** egress policy that permits exactly one loopback HTTP origin.

Production has one policy (:class:`nervos_mcp.policy.egress.StrictEgressPolicy`) and it refuses
plain ``http://`` outright, because the only HTTP endpoint NervOS ever tolerates is a loopback test
server and production is by definition not that. The integration tests therefore inject *this*
object
through the ordinary :class:`~nervos_mcp.policy.egress.EgressPolicy` port, which is exactly the seam
the production module documents.

It lives here, under ``tests/support/``, and is never importable from ``nervos_mcp``: no production
composition path can reach it, and ``test_egress_policy.py`` asserts that the production module
contains no permissive policy and no environment variable that could select one.
"""

from __future__ import annotations

from collections.abc import Iterable

from nervos_mcp.errors import McpConfigurationError, McpErrorCode
from nervos_mcp.policy.egress import EgressTarget, normalize_origin, parse_endpoint

# The one host a test server is allowed to bind. A loopback literal is required, not accepted as a
# side effect of resolving a name: a name that resolved to loopback would still be refused here.
LOOPBACK_HOST = "127.0.0.1"


class LoopbackEgressPolicy:
    """Permit ``http://127.0.0.1:<port>`` for a fixed set of ports, and nothing else.

    This is deliberately not configurable from the environment and not exported by
    :mod:`nervos_mcp.policy.egress`. It exists only so the fake server's port, chosen at test time,
    can be dialled without weakening the production policy.
    """

    def __init__(self, ports: Iterable[int]) -> None:
        self._ports = frozenset(int(port) for port in ports)

    def validate(self, endpoint: str) -> EgressTarget:
        """Approve one loopback HTTP endpoint, or refuse it with the production refusal code."""
        scheme, host, port = parse_endpoint(endpoint)
        if scheme != "http" or host != LOOPBACK_HOST or port not in self._ports:
            raise McpConfigurationError(McpErrorCode.ORIGIN_REFUSED)
        return EgressTarget(
            origin=normalize_origin(scheme, host, port),
            host=host,
            port=port,
            addresses=(host,),
        )


__all__ = ["LOOPBACK_HOST", "LoopbackEgressPolicy"]
