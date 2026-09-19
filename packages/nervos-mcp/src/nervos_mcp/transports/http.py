"""The Streamable HTTP transport: one audited origin, and the SDK's own same-origin redirect rule.

The SDK owns this wire protocol, and it already refuses a redirect that leaves the origin of the
request just sent, refuses a ``Location`` that carries userinfo of its own, and refuses a redirect
that would change the method and so drop the message. D5 preserves that behaviour rather than
building a second redirect rule beside it, which is why nothing here inspects a ``Location``
header.

What this module adds is the decision the SDK cannot make -- whether this process may dial the
origin *at all* -- and the credential, which must travel as a header on the client rather than
inside the URL. Both are applied here, the last moment before a socket exists.

**Only public APIs are used.** The HTTP client is an ordinary ``httpx2.AsyncClient``, built directly
rather than through an SDK helper, because that helper is not part of the SDK's public surface. The
SDK's own client factory would be the only alternative and it is private, so building the client
ourselves is what keeps this module free of private imports. This function owns that client end to
end: it is created here and closed here, so the object holding the bearer header has a lifetime that
ends inside the transport.

Timeouts are explicit and bounded. They are a lower-level backstop only: D4 owns the execution
deadline, and its outer timeout always wins.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx2
from mcp.client import Transport
from mcp.client.streamable_http import streamable_http_client

from nervos_mcp.operator_config import SecretValue
from nervos_mcp.policy.egress import EgressPolicy

# Connect/write/pool are bounded tightly because a socket that cannot be established should fail
# fast. The read timeout is generous because a server may legitimately hold an event stream open
# while a tool runs; the outer D4 deadline is what actually bounds a call.
_CONNECT_TIMEOUT_SECONDS = 30.0
_READ_TIMEOUT_SECONDS = 300.0


class _ClientOwnedTransport:
    """A ``Transport`` that closes the HTTP client it was given.

    The SDK's ``streamable_http_client`` does not close a client it did not create, so the ownership
    has to live somewhere. It lives here, on the object the ``Client`` enters and exits, which makes
    "the client is closed exactly when the session ends" a property of the type rather than a
    comment.
    """

    __slots__ = ("_client", "_context", "_endpoint")

    def __init__(self, endpoint: str, client: httpx2.AsyncClient) -> None:
        self._endpoint = endpoint
        self._client = client
        self._context: Any = None

    async def __aenter__(self) -> Any:
        await self._client.__aenter__()
        self._context = streamable_http_client(self._endpoint, http_client=self._client)
        return await self._context.__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._context is not None:
                await self._context.__aexit__(exc_type, exc, tb)
        finally:
            self._context = None
            await self._client.__aexit__(exc_type, exc, tb)


def http_transport(
    endpoint: str,
    policy: EgressPolicy,
    *,
    credential: SecretValue | None = None,
) -> Transport:
    """Validate one endpoint, build its HTTP client, and return the transport.

    The egress decision raises before any socket exists, so a refused origin is always a
    pre-dispatch failure rather than a mid-call one.
    """
    policy.validate(endpoint)
    headers = None if credential is None else {"Authorization": f"Bearer {credential.reveal()}"}
    client = httpx2.AsyncClient(
        headers=headers,
        timeout=httpx2.Timeout(_CONNECT_TIMEOUT_SECONDS, read=_READ_TIMEOUT_SECONDS),
    )
    return _ClientOwnedTransport(endpoint, client)


__all__ = ["http_transport"]
