"""Resolve and pin each catalog connection while preserving original TLS hostname."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import SOCKET_OPTION


class MarketplaceNetworkBackend(AutoBackend):
    def __init__(self, host: str, private: bool, development_loopback: bool) -> None:
        self.host, self.private, self.development_loopback = host, private, development_loopback

    def permitted(self, value: str) -> bool:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if address.is_loopback:
            return self.private or self.development_loopback
        if (
            address.is_unspecified
            or address.is_multicast
            or address.is_link_local
            or address.is_reserved
        ):
            return False
        return (
            address.is_global
            or (self.private and address.is_private)
            or (self.development_loopback and address.is_loopback)
        )

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if host != self.host:
            raise httpcore.ConnectError("Marketplace connection origin refused")
        async with asyncio.timeout(timeout or 10):
            addresses = await asyncio.get_running_loop().getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
            candidates = list(dict.fromkeys(str(item[4][0]) for item in addresses))
            if not candidates or not all(self.permitted(candidate) for candidate in candidates):
                raise httpcore.ConnectError("Marketplace connection address refused")
            # The network backend receives the numeric address; TLS above this seam keeps host.
            return await super().connect_tcp(
                candidates[0],
                port,
                timeout,
                local_address,
                socket_options,
            )


def marketplace_transport(
    host: str, private: bool, development_loopback: bool
) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
    # HTTPX has no public network_backend parameter. Keep this pinned adaptation in one place.
    transport._pool._network_backend = MarketplaceNetworkBackend(  # pyright: ignore[reportPrivateUsage]
        host,
        private,
        development_loopback,
    )
    return transport
