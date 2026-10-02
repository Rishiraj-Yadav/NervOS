"""Configured-origin modes and connection-bound DNS/address checks."""

import asyncio
import socket

import httpcore
import pytest
from nervos_api.application.marketplace_discovery import MarketplaceDiscoveryService
from nervos_api.application.marketplace_transport import MarketplaceNetworkBackend


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "::1", "::ffff:127.0.0.1", "10.0.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1"],
)
def test_public_origin_refuses_non_public_addresses(address: str) -> None:
    assert not MarketplaceNetworkBackend("marketplace.example", False, False).permitted(address)


def test_private_and_development_modes_are_explicit() -> None:
    private = MarketplaceNetworkBackend("private.example", True, False)
    development = MarketplaceNetworkBackend("localhost", False, True)
    assert private.permitted("10.0.0.1")
    assert not private.permitted("169.254.169.254")
    assert development.permitted("127.0.0.1")
    assert development.permitted("::1")
    assert not development.permitted("10.0.0.1")
    with pytest.raises(ValueError, match="development/test"):
        MarketplaceDiscoveryService("http://localhost:9000")
    MarketplaceDiscoveryService("http://localhost:9000", development=True)


def test_dns_rebinding_is_refused_at_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        async def addresses(*args: object, **kwargs: object):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", addresses)
        with pytest.raises(httpcore.ConnectError, match="address refused"):
            await MarketplaceNetworkBackend("marketplace.example", False, False).connect_tcp(
                "marketplace.example",
                443,
            )
        with pytest.raises(httpcore.ConnectError, match="origin refused"):
            await MarketplaceNetworkBackend("marketplace.example", False, False).connect_tcp(
                "another.example",
                443,
            )

    asyncio.run(run())
