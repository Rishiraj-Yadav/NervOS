"""The egress policy: what a NervOS process is allowed to open a socket to.

ADR 0016 gives the Worker a network position a user's configuration must not be able to aim. This
module is the whole of that decision, and it is deliberately **two independent tests** because they
answer different questions and neither implies the other:

* *Is this destination one the operator published?* -- an exact origin match against
  ``NERVOS_MCP_ALLOWED_ORIGINS``.
* *Is this destination one that must never be dialled from here, whatever anyone published?* -- a
  check over the **resolved addresses** against loopback, private, link-local, multicast,
  unspecified and cloud-metadata space.

The second test is not an allowlist feature and the allowlist cannot satisfy it. Adding
``http://169.254.169.254`` to the operator's origins does not make it reachable, because the address
class is refused before the allowlist is consulted for the transport's benefit. That ordering is the
point: the failure mode this guards against is a well-meaning operator widening an allowlist.

**What is deliberately not claimed.** Resolving a hostname here proves nothing about the address the
HTTP client will connect to a moment later; a name can answer differently to the two lookups
(ADR 0016 records this as an accepted Stage-H limitation, not a Stage-D guarantee). The policy is
therefore re-applied immediately before a transport is built, which narrows the window without
closing it, and nothing in NervOS describes the window as closed.

**What is not here.** There is no permissive implementation, no environment variable, and no hidden
flag that relaxes the forbidden-address classes. The loopback-permitting policy the tests need lives
under ``packages/nervos-mcp/tests/support/`` and is injected by those tests, so it cannot be
instantiated by any production composition path.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from nervos_mcp.errors import McpConfigurationError, McpErrorCode

# The port an origin without an explicit one uses. Normalisation makes `https://host` and
# `https://host:443` the same origin, because they are the same destination.
_DEFAULT_PORTS: dict[str, int] = {"https": 443, "http": 80}

# The one scheme production accepts. Plain HTTP is refused outright rather than refused-for-remote,
# because the only HTTP endpoint NervOS ever tolerates is a loopback test server, and production is
# by definition not that -- so "http is allowed for loopback" would be a clause whose only caller is
# a test. Tests inject a policy instead.
_REQUIRED_SCHEME = "https"

# Hostnames that name cloud instance-metadata services. These are refused as names *and*, once
# resolved, as addresses, because a name can be pointed at the metadata IP by DNS alone.
_FORBIDDEN_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "metadata",
    }
)

# The link-local block that carries the IPv4 and IPv6 metadata endpoints.
_METADATA_ADDRESSES = frozenset({"169.254.169.254", "fd00:ec2::254"})


def default_address_resolver(host: str, port: int) -> Sequence[str]:
    """Resolve ``host`` to every address family the platform reports.

    Every address is returned, not the one that would be connected to: a name answering with both a
    permitted and a forbidden address is refused, because which one a client picks is not something
    this policy can control.
    """
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return tuple(dict.fromkeys(str(info[4][0]) for info in infos))


class AddressResolver(Protocol):
    """Resolve a hostname to every address it currently answers with."""

    def __call__(self, host: str, port: int) -> Sequence[str]: ...


@dataclass(frozen=True, slots=True)
class EgressTarget:
    """One destination the policy approved: its normalised origin, and where it resolved."""

    origin: str
    host: str
    port: int
    addresses: tuple[str, ...]


class EgressPolicy(Protocol):
    """Decide whether one endpoint may be dialled, and to which addresses.

    Returns the approved target when the endpoint is permitted and raises
    :class:`~nervos_mcp.errors.McpConfigurationError` with a static code when it is not. It is a
    Protocol so tests can substitute a loopback-permitting policy without production code growing a
    relaxation; the composition root is the only place that chooses which one is built.
    """

    def validate(self, endpoint: str) -> EgressTarget: ...


def _forbidden_reason(address: str) -> str | None:
    """Classify one literal address, or ``None`` when it is an ordinary routable unicast address.

    IPv4-mapped IPv6 is unwrapped first: ``::ffff:192.168.1.1`` reaches the same host as
    ``192.168.1.1``, so a check that only looked at the IPv6 form would pass a private address.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return "unparseable"
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    if address in _METADATA_ADDRESSES:
        return "cloud-metadata"
    if parsed.is_loopback:
        return "loopback"
    if parsed.is_private:
        return "private"
    if parsed.is_link_local:
        return "link-local"
    if parsed.is_multicast:
        return "multicast"
    if parsed.is_unspecified:
        return "unspecified"
    if parsed.is_reserved:
        return "reserved"
    return None


def normalize_origin(scheme: str, host: str, port: int) -> str:
    """Return the canonical ``scheme://host[:port]`` form an allowlist entry is compared against."""
    default = _DEFAULT_PORTS.get(scheme)
    suffix = "" if default == port else f":{port}"
    return f"{scheme}://{host.lower()}{suffix}"


def parse_endpoint(endpoint: str) -> tuple[str, str, int]:
    """Split an endpoint into (scheme, host, port), refusing anything not strictly absolute.

    A URL carries authority in places other than its host, and each of those is a way to smuggle a
    destination past an origin comparison. They are refused by name rather than normalised away:
    a fragment, a userinfo component and an unparseable authority are all configuration mistakes,
    and silently repairing one would hide the attempt.
    """
    parts = urlsplit(endpoint)
    if parts.scheme not in _DEFAULT_PORTS:
        raise McpConfigurationError(McpErrorCode.ORIGIN_REFUSED)
    if parts.fragment:
        raise McpConfigurationError(McpErrorCode.ORIGIN_REFUSED)
    if parts.username is not None or parts.password is not None:
        raise McpConfigurationError(McpErrorCode.ORIGIN_REFUSED)
    host = parts.hostname
    if host is None or not host:
        raise McpConfigurationError(McpErrorCode.ORIGIN_REFUSED)
    try:
        port = parts.port
    except ValueError as error:
        # A non-numeric or out-of-range port is an unusable authority, not a host with no port.
        raise McpConfigurationError(McpErrorCode.ORIGIN_REFUSED) from error
    return parts.scheme, host, _DEFAULT_PORTS[parts.scheme] if port is None else port


class StrictEgressPolicy:
    """The only production egress policy: an exact-origin allowlist plus address refusals."""

    def __init__(
        self,
        allowed_origins: frozenset[str],
        *,
        resolve: AddressResolver = default_address_resolver,
    ) -> None:
        self._allowed_origins = frozenset(
            _normalize_allowlist(origin) for origin in allowed_origins
        )
        self._resolve = resolve

    def validate(self, endpoint: str) -> EgressTarget:
        scheme, host, port = parse_endpoint(endpoint)
        if scheme != _REQUIRED_SCHEME:
            raise McpConfigurationError(McpErrorCode.ORIGIN_REFUSED)
        origin = normalize_origin(scheme, host, port)
        # An empty allowlist is the default, and it denies every remote origin: a Worker that has
        # not been told what it may reach reaches nothing.
        if origin not in self._allowed_origins:
            raise McpConfigurationError(McpErrorCode.ORIGIN_REFUSED)
        if host.lower() in _FORBIDDEN_HOSTNAMES:
            raise McpConfigurationError(McpErrorCode.ORIGIN_REFUSED)
        addresses = tuple(self._resolve(host, port))
        if not addresses:
            raise McpConfigurationError(McpErrorCode.ORIGIN_REFUSED)
        for address in addresses:
            if _forbidden_reason(address) is not None:
                raise McpConfigurationError(McpErrorCode.ORIGIN_REFUSED)
        return EgressTarget(origin=origin, host=host, port=port, addresses=addresses)


def _normalize_allowlist(origin: str) -> str:
    """Normalise one operator-supplied allowlist entry, or refuse the entry outright.

    The entry is parsed by the same function an endpoint is, so an allowlist and a connection cannot
    disagree about what an origin is. An entry that cannot be parsed is a configuration error rather
    than a dead string, because a silently ignored entry reads to an operator as a granted origin.
    """
    scheme, host, port = parse_endpoint(origin)
    return normalize_origin(scheme, host, port)


def parse_allowed_origins(raw: str) -> frozenset[str]:
    """Parse the operator's ``NERVOS_MCP_ALLOWED_ORIGINS`` value into a normalised origin set."""
    entries = [entry.strip() for entry in raw.split(",")]
    return frozenset(entry for entry in entries if entry)


__all__ = [
    "AddressResolver",
    "EgressPolicy",
    "EgressTarget",
    "StrictEgressPolicy",
    "default_address_resolver",
    "normalize_origin",
    "parse_allowed_origins",
    "parse_endpoint",
]
