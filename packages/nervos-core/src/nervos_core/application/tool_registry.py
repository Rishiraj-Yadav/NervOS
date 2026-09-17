"""Provider-neutral tool ports, the registry, and durable definition persistence.

Four things live here, and nothing else:

* the two adapter ports (:class:`ToolSource`, :class:`ToolExecutor`) and :class:`ToolResult`;
* :class:`ToolRegistry` -- static registration of source/executor pairs keyed by durable source
  identity, so several MCP connections coexist without a redesign;
* the :class:`ToolDefinitionPersistence` protocol, implemented in infrastructure;
* the typed failure a tool raises for a condition it can classify locally.

**What the registry is not.** It is not catalog assembly, not a permission authority, not provider
conversion, not an MCP connection manager, and not a plugin system. There is no entry-point
scanning, no ``importlib`` discovery and no Stage G package machinery -- registration is explicit
and static.
The call-time permission evaluator is deliberately **not** duplicated here; D4 composes the registry
with D2's evaluator.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, cast

from nervos_core.domain.tools import (
    DefinitionStatus,
    InvalidToolDefinition,
    JsonValue,
    RiskHints,
    ToolDescriptor,
    ToolSourceKind,
    ToolSourceRef,
    require_json_value,
)


class ToolFailureReason(StrEnum):
    """A closed, provider-neutral vocabulary for failures a tool classifies locally.

    Deliberately small, and it grows for no single tool. A condition specific to one built-in is
    classified under an existing member rather than adding one here.
    """

    ARGUMENTS_INVALID = "arguments_invalid"
    RESULT_TOO_LARGE = "result_too_large"
    RESULT_UNSUPPORTED = "result_unsupported"
    INTERNAL = "internal"


class ToolExecutionFailure(Exception):
    """A deterministic, locally classified, safe tool failure.

    The message is bounded static text safe to record and safe to return to a model; it never
    carries a credential, a raw provider payload or an argument value.
    """

    def __init__(self, reason: ToolFailureReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class DuplicateToolSource(ValueError):
    """Raised when one exact source identity is registered twice."""


class UnknownToolSource(LookupError):
    """Raised when a source identity has no registration."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The supported output of one tool call: text plus JSON-safe structured content.

    There is no image, audio or resource variant, and no streaming. An unsupported output type is a
    classified failure, never a stringified or base64 blob pushed into a model's context.
    """

    text: str
    structured: JsonValue | None = None

    def __post_init__(self) -> None:
        if self.structured is not None:
            # A built-in emitting a non-JSON value is a defect in our own code, so this raises
            # rather than returning a verdict.
            require_json_value(self.structured, path="$.structured")


class ToolSource(Protocol):
    """Discovers the tools a source currently offers, as durable descriptors."""

    async def list_tools(self) -> Sequence[ToolDescriptor]: ...


class ToolExecutor(Protocol):
    """Executes one call against one already-authorized durable descriptor."""

    async def execute(
        self, descriptor: ToolDescriptor, arguments: Mapping[str, JsonValue]
    ) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class ToolDefinitionMaterial:
    """The material columns of one definition, before or after persistence. Carries **no** id.

    A :class:`ToolDescriptor` requires durable identity, so it cannot be passed to an insert -- the
    id does not exist until the row is written. This is the honest pre-INSERT shape, and it is why
    the descriptor needs no "possibly unpersisted" variant.
    """

    source_kind: ToolSourceKind
    source_id: int | None
    upstream_name: str
    model_name: str
    display_name: str
    description: str
    input_schema: str
    output_schema: str | None
    fingerprint: str
    status: DefinitionStatus
    risk_hints: RiskHints

    @property
    def source_ref(self) -> ToolSourceRef:
        return ToolSourceRef(self.source_kind, self.source_id)


@dataclass(frozen=True, slots=True)
class PersistedToolDefinition:
    """One durable definition row: its real id plus its material."""

    tool_definition_id: int
    material: ToolDefinitionMaterial

    def to_descriptor(self) -> ToolDescriptor:
        input_schema = json.loads(self.material.input_schema)
        raw_output_schema = (
            None if self.material.output_schema is None else json.loads(self.material.output_schema)
        )
        # schema validation guarantees both, so these are recorded facts rather than hopes.
        if not isinstance(input_schema, dict):  # pragma: no cover
            raise InvalidToolDefinition("persisted input schema is not an object")
        if raw_output_schema is not None and not isinstance(raw_output_schema, dict):
            # pragma: no cover - schema validation guarantees it
            raise InvalidToolDefinition("persisted output schema is not an object")
        return ToolDescriptor(
            tool_definition_id=self.tool_definition_id,
            upstream_name=self.material.upstream_name,
            model_name=self.material.model_name,
            source_kind=self.material.source_kind,
            source_id=self.material.source_id,
            display_name=self.material.display_name,
            description=self.material.description,
            input_schema=cast("dict[str, JsonValue]", input_schema),
            output_schema=(
                None
                if raw_output_schema is None
                else cast("dict[str, JsonValue]", raw_output_schema)
            ),
            risk_hints=self.material.risk_hints,
            fingerprint=self.material.fingerprint,
        )


class ToolDefinitionPersistence(Protocol):
    """Durable tool-definition reads and writes.

    Implemented in infrastructure; the application layer depends only on this shape. Every operation
    is owner-agnostic because definitions are source configuration -- the *authority* over a
    definition is a grant, which is D2's table and is never touched from here.
    """

    def find_builtin(self, *, upstream_name: str) -> PersistedToolDefinition | None: ...

    def insert(self, material: ToolDefinitionMaterial, *, now: datetime) -> int: ...

    def update_material(
        self,
        tool_definition_id: int,
        *,
        material: ToolDefinitionMaterial,
        now: datetime,
    ) -> None: ...

    def get(self, tool_definition_id: int) -> PersistedToolDefinition | None: ...

    def list_for_source(
        self, *, source_ref: ToolSourceRef
    ) -> tuple[PersistedToolDefinition, ...]: ...


class ToolRegistry:
    """Static registration of source/executor pairs, keyed by durable source identity.

    Keying by :class:`ToolSourceRef` rather than by kind alone is what lets ``(MCP, 12)`` and
    ``(MCP, 27)`` coexist beside ``(BUILTIN, None)``, so D5 needs no redesign. D3 registers only
    ``(BUILTIN, None)``.
    """

    def __init__(self) -> None:
        self._sources: dict[ToolSourceRef, ToolSource] = {}
        self._executors: dict[ToolSourceRef, ToolExecutor] = {}

    def register(
        self,
        *,
        source_ref: ToolSourceRef,
        source: ToolSource,
        executor: ToolExecutor,
    ) -> None:
        if source_ref in self._sources:
            raise DuplicateToolSource(source_ref)
        self._sources[source_ref] = source
        self._executors[source_ref] = executor

    def source(self, source_ref: ToolSourceRef) -> ToolSource:
        try:
            return self._sources[source_ref]
        except KeyError as error:
            raise UnknownToolSource(source_ref) from error

    def executor(self, source_ref: ToolSourceRef) -> ToolExecutor:
        try:
            return self._executors[source_ref]
        except KeyError as error:
            raise UnknownToolSource(source_ref) from error

    def source_refs(self) -> tuple[ToolSourceRef, ...]:
        return tuple(self._sources)
