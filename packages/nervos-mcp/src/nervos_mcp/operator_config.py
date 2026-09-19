"""Operator-owned configuration: the only source of a command line, and the only source of a secret.

Two things live here, and they are here for the same reason: **a user configures a name, never a
mechanism.**

A connection row may contain an opaque ``server_key`` and an opaque ``credential_ref``. It may not
contain an executable, an argument vector, a working directory, an environment mapping, a PATH
entry, or the name of an environment variable. Each of those would let whoever writes the row decide
what code this process runs or which secret it hands over, and a database row is user input. The
translation from *name* to *mechanism* therefore happens here, against configuration only the
operator can write, and a name with no entry is refused rather than defaulted.

The credential side has the same shape with one extra rule: an alias is bound to the targets it may
be used for. An alias that is offered for one origin and presented to another is refused, so
possessing a connection row is not enough to spend a credential somewhere new.

**The secret itself is never stored here.** :class:`CredentialAliasSpec` holds the *name* of the
environment variable; the value is read at :meth:`McpOperatorConfig.resolve_secret`, which callers
invoke at the last practical moment before a transport is built. Nothing in this module keeps it,
formats it, or exposes it through a ``repr``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from nervos_mcp.errors import McpConfigurationError, McpErrorCode
from nervos_mcp.policy.egress import parse_allowed_origins

# The only authentication scheme Stage D supports. A spec naming anything else is a configuration
# error rather than a silently-ignored alias, because an ignored alias reads as a granted one.
_SUPPORTED_SCHEME = "bearer"


class SecretValue:
    """A credential value that cannot leak through formatting.

    This is a deliberate second line of defence rather than the only one: the value is never placed
    in durable state, never logged, and never returned to a caller, so a ``repr`` that masks it is
    protecting against an accident -- an f-string in a future traceback, say -- and not carrying the
    design. :meth:`reveal` is the single named way to obtain the string, so an occurrence of the
    plaintext in a review is always an explicit act.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the credential text. The only method that does."""
        return self._value

    def __repr__(self) -> str:
        return "SecretValue('***')"

    def __str__(self) -> str:
        return "***"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SecretValue) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True, slots=True)
class StdioServerSpec:
    """One operator-declared stdio server.

    ``executable`` must be an absolute path, and it is the operator's decision -- not the row's, and
    not the caller's -- what process runs. The child is started with this exact argument vector and
    no shell, so no part of a row can become shell syntax.
    """

    server_key: str
    executable: str
    args: tuple[str, ...] = ()
    working_dir: str | None = None
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    credential_aliases: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.server_key or not self.server_key.isascii():
            raise ValueError("server key must be non-empty ASCII")
        # A relative executable would be resolved against the worker's working directory, which is
        # not the operator's decision surface. Requiring an absolute path makes "which binary ran"
        # answerable by reading the configuration alone.
        if not Path(self.executable).is_absolute():
            raise ValueError("stdio executable must be an absolute path")


@dataclass(frozen=True, slots=True)
class CredentialAliasSpec:
    """One operator-declared credential alias and the targets it may be spent on.

    ``env_var`` is the *name* of an environment variable. It is never persisted, never returned in a
    response, and never accepted from a request -- a request may only name the alias.
    """

    alias: str
    env_var: str
    allowed_targets: frozenset[str] = frozenset()
    scheme: str = _SUPPORTED_SCHEME

    def __post_init__(self) -> None:
        if not self.alias or not self.alias.isascii():
            raise ValueError("credential alias must be non-empty ASCII")
        if self.scheme != _SUPPORTED_SCHEME:
            raise ValueError(f"unsupported credential scheme: {self.scheme!r}")


@dataclass(frozen=True, slots=True)
class McpOperatorConfig:
    """Everything the operator declared, resolved by name and never by mechanism."""

    stdio_servers: Mapping[str, StdioServerSpec] = field(default_factory=dict[str, StdioServerSpec])
    credential_aliases: Mapping[str, CredentialAliasSpec] = field(
        default_factory=dict[str, CredentialAliasSpec]
    )
    allowed_origins: frozenset[str] = frozenset()

    def stdio_server(self, server_key: str) -> StdioServerSpec:
        """Return the declared stdio server, or refuse an undeclared key.

        There is no default and no fallback: an undeclared key is a refusal, which is what stops an
        unrecognised name from silently becoming *some* command.
        """
        try:
            return self.stdio_servers[server_key]
        except KeyError as error:
            raise McpConfigurationError(McpErrorCode.SERVER_KEY_UNKNOWN) from error

    def credential_alias(self, alias: str) -> CredentialAliasSpec:
        """Return the declared alias, or refuse an undeclared one."""
        try:
            return self.credential_aliases[alias]
        except KeyError as error:
            raise McpConfigurationError(McpErrorCode.CREDENTIAL_ALIAS_NOT_AVAILABLE) from error

    def check_alias_target(self, alias: str, target: str) -> CredentialAliasSpec:
        """Prove an alias exists *and* is bound to ``target``, before a connection is stored.

        Refusing at create time is what keeps a stored ``credential_ref`` from naming an audience it
        was never allowed to address.
        """
        spec = self.credential_alias(alias)
        if target not in spec.allowed_targets:
            raise McpConfigurationError(McpErrorCode.CREDENTIAL_ALIAS_NOT_AVAILABLE)
        return spec

    def resolve_secret(
        self,
        alias: str,
        target: str,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> SecretValue:
        """Re-prove the alias binding and read the secret, at the last practical moment.

        Called immediately before a transport is constructed, so a configuration change that removes
        an alias takes effect on the next call rather than at the next restart. A blank or absent
        variable is the same refusal as an unknown alias: both mean the credential is not available,
        and neither is a condition a caller can retry into existence.
        """
        spec = self.check_alias_target(alias, target)
        source: Mapping[str, str] = os.environ if environ is None else environ
        value = source.get(spec.env_var)
        if value is None or not value.strip():
            raise McpConfigurationError(McpErrorCode.CREDENTIAL_ALIAS_NOT_AVAILABLE)
        return SecretValue(value)


__all__ = [
    "CredentialAliasSpec",
    "McpOperatorConfig",
    "SecretValue",
    "StdioServerSpec",
    "load_operator_config",
]


def _decode_object(raw: str, what: str) -> dict[str, Any]:
    """Decode one operator JSON declaration, refusing anything that is not an object.

    A malformed declaration raises rather than being skipped: a silently dropped entry reads to an
    operator exactly like a granted one, and the difference is a security property.
    """
    decoded: Any = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError(f"{what} must be a JSON object")
    return cast("dict[str, Any]", decoded)


def _str_items(value: Any, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a JSON array")
    return tuple(str(item) for item in cast("list[Any]", value))


def _str_map(value: Any, what: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be a JSON object")
    return {str(key): str(item) for key, item in cast("dict[str, Any]", value).items()}


def load_operator_config(
    *,
    allowed_origins: str,
    stdio_servers_json: str = "",
    credential_aliases_json: str = "",
) -> McpOperatorConfig:
    """Build the operator configuration from the environment-shaped inputs a process receives.

    Everything defaults to empty, and an empty configuration refuses: no origin may be dialled, no
    stdio server key resolves, and no credential alias exists. That is the fail-closed direction --
    a process whose operator has configured nothing reaches nothing -- which is why the defaults
    are not "allow local" or "inherit".
    """
    servers: dict[str, StdioServerSpec] = {}
    if stdio_servers_json.strip():
        for key, value in _decode_object(stdio_servers_json, "mcp stdio servers").items():
            spec = _decode_object(json.dumps(value), f"stdio server {key!r}")
            servers[key] = StdioServerSpec(
                server_key=key,
                executable=str(spec["executable"]),
                args=_str_items(spec.get("args"), "args"),
                working_dir=None if spec.get("working_dir") is None else str(spec["working_dir"]),
                env=_str_map(spec.get("env"), "env"),
                credential_aliases=frozenset(
                    _str_items(spec.get("credential_aliases"), "credential_aliases")
                ),
            )

    aliases: dict[str, CredentialAliasSpec] = {}
    if credential_aliases_json.strip():
        for key, value in _decode_object(credential_aliases_json, "mcp credential aliases").items():
            spec = _decode_object(json.dumps(value), f"credential alias {key!r}")
            aliases[key] = CredentialAliasSpec(
                alias=key,
                env_var=str(spec["env_var"]),
                allowed_targets=frozenset(
                    _str_items(spec.get("allowed_targets"), "allowed_targets")
                ),
                scheme=str(spec.get("scheme", _SUPPORTED_SCHEME)),
            )

    return McpOperatorConfig(
        stdio_servers=servers,
        credential_aliases=aliases,
        allowed_origins=parse_allowed_origins(allowed_origins),
    )
