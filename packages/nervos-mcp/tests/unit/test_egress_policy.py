"""The egress policy: two independent tests, and no way to widen either at runtime.

The refusals below split along the module's own seam. One group is about *which origin the operator
published*; the other is about *which address must never be dialled*, and it is proved with an
injected resolver so no test depends on DNS. The last group pins the policy's shape: one production
implementation, no permissive alternative, and no environment variable that could produce one.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable, Sequence
from types import ModuleType

import nervos_mcp.policy.egress as production_egress
import pytest
from nervos_mcp.errors import McpConfigurationError, McpErrorCode
from nervos_mcp.policy.egress import AddressResolver, EgressTarget, StrictEgressPolicy

from ..support.egress import LOOPBACK_HOST, LoopbackEgressPolicy

PUBLIC_ADDRESS = "93.184.216.34"
ALLOWED_ORIGIN = "https://docs.example"
ALLOWED_ENDPOINT = f"{ALLOWED_ORIGIN}/mcp"

# Every address class the policy promises to refuse, whichever name resolved to it.
FORBIDDEN_ADDRESSES = [
    "127.0.0.1",
    "::1",
    "10.0.0.1",
    "192.168.1.1",
    "172.16.0.1",
    "169.254.169.254",
    "fe80::1",
    "224.0.0.1",
    "0.0.0.0",
    "::ffff:192.168.1.1",
]


def _resolver(*addresses: str) -> AddressResolver:
    """A resolver that answers with exactly the addresses a test names."""

    def resolve(host: str, port: int) -> Sequence[str]:
        return tuple(addresses)

    return resolve


def _policy(
    origins: Iterable[str], addresses: Sequence[str] = (PUBLIC_ADDRESS,)
) -> StrictEgressPolicy:
    return StrictEgressPolicy(frozenset(origins), resolve=_resolver(*addresses))


def test_accepts_an_allowlisted_origin_that_resolves_to_a_public_address() -> None:
    target = _policy([ALLOWED_ORIGIN]).validate(ALLOWED_ENDPOINT)

    assert target == EgressTarget(
        origin=ALLOWED_ORIGIN,
        host="docs.example",
        port=443,
        addresses=(PUBLIC_ADDRESS,),
    )


def test_refuses_an_origin_that_was_never_allowlisted() -> None:
    with pytest.raises(McpConfigurationError) as failure:
        _policy([ALLOWED_ORIGIN]).validate("https://other.example/mcp")

    assert failure.value.code is McpErrorCode.ORIGIN_REFUSED


def test_an_empty_allowlist_refuses_every_origin() -> None:
    with pytest.raises(McpConfigurationError) as failure:
        _policy([]).validate(ALLOWED_ENDPOINT)

    assert failure.value.code is McpErrorCode.ORIGIN_REFUSED


def test_refuses_plain_http_even_when_it_is_allowlisted() -> None:
    with pytest.raises(McpConfigurationError) as failure:
        _policy(["http://docs.example"]).validate("http://docs.example/mcp")

    assert failure.value.code is McpErrorCode.ORIGIN_REFUSED


def test_refuses_a_url_that_carries_userinfo() -> None:
    with pytest.raises(McpConfigurationError) as failure:
        _policy([ALLOWED_ORIGIN]).validate("https://user:password@docs.example/mcp")

    assert failure.value.code is McpErrorCode.ORIGIN_REFUSED


def test_refuses_a_url_that_carries_a_fragment() -> None:
    with pytest.raises(McpConfigurationError) as failure:
        _policy([ALLOWED_ORIGIN]).validate(f"{ALLOWED_ENDPOINT}#fragment")

    assert failure.value.code is McpErrorCode.ORIGIN_REFUSED


def test_refuses_a_cloud_metadata_hostname_on_the_allowlist() -> None:
    with pytest.raises(McpConfigurationError) as failure:
        _policy(["https://metadata.google.internal"]).validate(
            "https://metadata.google.internal/latest"
        )

    assert failure.value.code is McpErrorCode.ORIGIN_REFUSED


@pytest.mark.parametrize("address", FORBIDDEN_ADDRESSES)
def test_an_allowlisted_origin_is_still_refused_when_it_resolves_to_a_forbidden_address(
    address: str,
) -> None:
    """The allowlist cannot grant an address: publishing an origin is not enough to dial it."""
    policy = _policy([ALLOWED_ORIGIN], [address])

    with pytest.raises(McpConfigurationError) as failure:
        policy.validate(ALLOWED_ENDPOINT)

    assert failure.value.code is McpErrorCode.ORIGIN_REFUSED


def test_refuses_an_origin_that_resolves_to_nothing() -> None:
    with pytest.raises(McpConfigurationError) as failure:
        _policy([ALLOWED_ORIGIN], []).validate(ALLOWED_ENDPOINT)

    assert failure.value.code is McpErrorCode.ORIGIN_REFUSED


def _concrete_policy_names(module: ModuleType) -> list[str]:
    """Names of concrete classes defined in ``module`` that implement the policy port."""
    names: list[str] = []
    for name, value in vars(module).items():
        if name == "EgressPolicy" or not isinstance(value, type):
            continue
        if value.__module__ != module.__name__:
            continue
        if callable(getattr(value, "validate", None)):
            names.append(name)
    return sorted(names)


def test_production_egress_module_has_exactly_one_policy() -> None:
    assert _concrete_policy_names(production_egress) == ["StrictEgressPolicy"]


def test_production_egress_module_contains_no_permissive_policy() -> None:
    exported = getattr(production_egress, "__all__", ())
    lowered = " ".join(str(name).lower() for name in exported)

    assert "permissive" not in lowered
    assert "loopback" not in lowered
    assert not hasattr(production_egress, "LoopbackEgressPolicy")
    assert not hasattr(production_egress, "AllowAllEgressPolicy")


def test_no_environment_variable_can_reach_a_production_policy() -> None:
    """The module never reads the environment, so no variable can select or widen a policy."""
    source = inspect.getsource(production_egress)

    assert "os.environ" not in source
    assert "getenv" not in source


def test_an_environment_variable_cannot_widen_the_strict_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NERVOS_MCP_ALLOWED_ORIGINS", "http://127.0.0.1:8080")

    with pytest.raises(McpConfigurationError) as failure:
        _policy([]).validate("http://127.0.0.1:8080/mcp")

    assert failure.value.code is McpErrorCode.ORIGIN_REFUSED


def test_the_test_only_policy_permits_only_its_loopback_port() -> None:
    policy = LoopbackEgressPolicy({8080})

    target = policy.validate("http://127.0.0.1:8080/mcp")

    assert target == EgressTarget(
        origin="http://127.0.0.1:8080",
        host=LOOPBACK_HOST,
        port=8080,
        addresses=(LOOPBACK_HOST,),
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:8081/mcp",
        "https://127.0.0.1:8080/mcp",
        "http://localhost:8080/mcp",
        "http://192.168.1.10:8080/mcp",
        "http://[::1]:8080/mcp",
    ],
)
def test_the_test_only_policy_refuses_everything_else(endpoint: str) -> None:
    policy = LoopbackEgressPolicy({8080})

    with pytest.raises(McpConfigurationError) as failure:
        policy.validate(endpoint)

    assert failure.value.code is McpErrorCode.ORIGIN_REFUSED
