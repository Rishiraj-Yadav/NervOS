"""Approved public runtime-integration contracts, without infrastructure dependencies."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

from nervos_core.application.clock import Clock
from nervos_core.domain.packages import InvalidPackageManifest, PackageManifest

EXTENSION_KEY = "x-nervos-runtime-integration"
MEMORY_MODES = frozenset({"manual", "review", "automatic_private"})
WORKFLOW_PROTOCOL = "workflow-v1"


class IntegrationNotFound(LookupError):
    """Missing and foreign resources deliberately share one error."""


class IntegrationConflict(ValueError):
    """A stale policy, binding or suggestion must be refreshed."""


@dataclass(frozen=True, slots=True)
class McpRequirement:
    alias: str
    upstream_name: str
    input_schema_sha256: str
    required: bool


@dataclass(frozen=True, slots=True)
class PackageIntegration:
    structured_context: bool = False
    tools: tuple[McpRequirement, ...] = ()
    # ADR 0039: the package requires the durable-workflow host feature. This is a *demand*,
    # recorded at install time so the Worker can refuse an old host before any package code
    # runs, rather than discovering the gap inside a running entrypoint.
    workflow: bool = False


def package_integration(manifest: PackageManifest) -> PackageIntegration:
    value = manifest.extensions.get(EXTENSION_KEY)
    if value is None:
        return PackageIntegration()
    if not isinstance(value, Mapping):
        raise InvalidPackageManifest("runtime integration must be a mapping")
    data = cast(Mapping[str, object], value)
    if (
        set(data) - {"version", "context", "mcp_tools", "workflow"}
        or type(data.get("version")) is not int
        or data.get("version") != 1
    ):
        raise InvalidPackageManifest("unsupported runtime integration version or fields")
    context = data.get("context", "legacy")
    if context not in ("legacy", "structured-v1"):
        raise InvalidPackageManifest("unsupported package context protocol")
    workflow = data.get("workflow", False)
    if workflow is False:
        pass
    elif workflow == WORKFLOW_PROTOCOL:
        workflow = True
    else:
        # Only the exact protocol string, or an explicit absence. `True` is not accepted:
        # a boolean would silently match whatever protocol the host happens to speak.
        raise InvalidPackageManifest("unsupported package workflow protocol")
    raw_tools = data.get("mcp_tools", [])
    if not isinstance(raw_tools, (list, tuple)) or len(cast(Sequence[object], raw_tools)) > 32:
        raise InvalidPackageManifest("mcp_tools must contain at most 32 declarations")
    tools: list[McpRequirement] = []
    for raw in cast(Sequence[object], raw_tools):
        if not isinstance(raw, Mapping):
            raise InvalidPackageManifest("MCP requirement must be a mapping")
        item = cast(Mapping[str, object], raw)
        if set(item) != {"alias", "upstream_name", "input_schema_sha256", "required"}:
            raise InvalidPackageManifest("MCP requirement fields are invalid")
        alias, upstream, digest = item["alias"], item["upstream_name"], item["input_schema_sha256"]
        if not isinstance(alias, str) or re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", alias) is None:
            raise InvalidPackageManifest("invalid portable MCP alias")
        if not isinstance(upstream, str) or not 1 <= len(upstream) <= 128 or "\x00" in upstream:
            raise InvalidPackageManifest("invalid MCP upstream name")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise InvalidPackageManifest("invalid MCP input schema digest")
        if (
            type(item["required"]) is not bool
            or alias in {t.alias for t in tools}
            or alias in (*manifest.tools.required, *manifest.tools.optional)
        ):
            raise InvalidPackageManifest("duplicate MCP alias or invalid required flag")
        tools.append(McpRequirement(alias, upstream, digest, item["required"]))
    return PackageIntegration(context == "structured-v1", tuple(tools), workflow)


@dataclass(frozen=True, slots=True)
class MemoryProposal:
    content: str
    scope: str = "agent"

    def __post_init__(self) -> None:
        if (
            self.scope not in {"user", "agent"}
            or not self.content.strip()
            or "\x00" in self.content
            or len(self.content.encode("utf-8")) > 2000
        ):
            raise ValueError("invalid bounded memory proposal")


def parse_memory_proposals(value: object) -> tuple[MemoryProposal, ...]:
    if not isinstance(value, list) or len(cast(list[object], value)) > 6:
        raise ValueError("memory proposals must be a list of at most six facts")
    result: list[MemoryProposal] = []
    for raw in cast(list[object], value):
        if not isinstance(raw, dict):
            raise ValueError("memory proposal must be an object")
        item = cast(dict[str, object], raw)
        if (
            set(item) - {"content", "scope"}
            or not isinstance(item.get("content"), str)
            or not isinstance(item.get("scope", "agent"), str)
        ):
            raise ValueError("invalid memory proposal fields")
        result.append(
            MemoryProposal(
                cast(str, item["content"]).strip(), cast(str, item.get("scope", "agent"))
            )
        )
    if sum(len(item.content.encode("utf-8")) for item in result) > 8000:
        raise ValueError("memory proposal batch exceeds its byte budget")
    return tuple(result)


class RuntimeIntegrationPersistence(Protocol):
    def agent_tools(self, owner: int, instance: int) -> dict[str, Any]: ...
    def bind_tool(
        self, owner: int, instance: int, alias: str, definition: int, now: datetime
    ) -> None: ...
    def unbind_tool(self, owner: int, instance: int, alias: str) -> None: ...
    def grant_tool(
        self, owner: int, instance: int, definition: int, action: str, now: datetime
    ) -> None: ...
    def tools(
        self, owner: int, connection: int | None, before_id: int | None
    ) -> dict[str, Any]: ...
    def policy(self, owner: int, instance: int) -> dict[str, Any]: ...
    def set_policy(
        self, owner: int, instance: int, mode: str, extraction: bool, revision: int, now: datetime
    ) -> dict[str, Any]: ...
    def suggestions(
        self, owner: int, instance: int | None, before_id: int | None
    ) -> dict[str, Any]: ...
    def decide_suggestion(
        self, owner: int, suggestion: int, approve: bool, now: datetime
    ) -> None: ...
    def run_context(self, owner: int, run: int) -> dict[str, Any]: ...
    def health(self, owner: int, now: datetime) -> dict[str, Any]: ...


class RuntimeIntegrationService:
    def __init__(self, persistence: RuntimeIntegrationPersistence, clock: Clock) -> None:
        self._store = persistence
        self._clock = clock

    def agent_tools(self, owner: int, instance: int) -> dict[str, Any]:
        return self._store.agent_tools(owner, instance)

    def bind_tool(self, owner: int, instance: int, alias: str, definition: int) -> None:
        self._store.bind_tool(owner, instance, alias, definition, self._clock())

    def unbind_tool(self, owner: int, instance: int, alias: str) -> None:
        self._store.unbind_tool(owner, instance, alias)

    def grant_tool(self, owner: int, instance: int, definition: int, action: str) -> None:
        if action not in {"grant", "revoke", "reconfirm"}:
            raise ValueError("invalid permission action")
        self._store.grant_tool(owner, instance, definition, action, self._clock())

    def tools(
        self, owner: int, connection: int | None = None, before_id: int | None = None
    ) -> dict[str, Any]:
        return self._store.tools(owner, connection, before_id)

    def policy(self, owner: int, instance: int) -> dict[str, Any]:
        return self._store.policy(owner, instance)

    def set_policy(
        self, owner: int, instance: int, mode: str, extraction: bool, revision: int
    ) -> dict[str, Any]:
        if mode not in MEMORY_MODES or revision < 0 or (mode == "manual" and extraction):
            raise ValueError("invalid memory policy")
        return self._store.set_policy(owner, instance, mode, extraction, revision, self._clock())

    def suggestions(
        self, owner: int, instance: int | None = None, before_id: int | None = None
    ) -> dict[str, Any]:
        return self._store.suggestions(owner, instance, before_id)

    def decide_suggestion(self, owner: int, suggestion: int, approve: bool) -> None:
        self._store.decide_suggestion(owner, suggestion, approve, self._clock())

    def run_context(self, owner: int, run: int) -> dict[str, Any]:
        return self._store.run_context(owner, run)

    def health(self, owner: int) -> dict[str, Any]:
        return self._store.health(owner, self._clock())
