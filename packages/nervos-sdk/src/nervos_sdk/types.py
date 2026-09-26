"""Transport-neutral public data contracts for NervOS agents."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, TypeAlias, cast

if TYPE_CHECKING:
    from nervos_sdk.ports import ModelPort, ToolPort

JSONScalar: TypeAlias = str | int | float | bool | None  # noqa: UP040
JSONValue: TypeAlias = (  # noqa: UP040
    JSONScalar | Mapping[str, "JSONValue"] | Sequence["JSONValue"]
)
ImmutableJSONValue: TypeAlias = (  # noqa: UP040
    JSONScalar | Mapping[str, "ImmutableJSONValue"] | tuple["ImmutableJSONValue", ...]
)

SDK_API_VERSION = "0.1"


def _freeze_json(value: JSONValue) -> ImmutableJSONValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    return tuple(_freeze_json(item) for item in value)


def _freeze_mapping(value: Mapping[str, JSONValue]) -> Mapping[str, ImmutableJSONValue]:
    return cast(
        "Mapping[str, ImmutableJSONValue]",
        MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()}),
    )


def _json_mapping() -> Mapping[str, JSONValue]:
    """An empty immutable JSON mapping default. Typed so strict mode sees no unknown generics."""
    return MappingProxyType({})


def _int_mapping() -> Mapping[str, int]:
    """An empty immutable token-usage mapping default, typed for the same reason."""
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """A provider-neutral model message."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """A narrow model-completion request passed through the SDK model port."""

    messages: Sequence[ModelMessage]
    model: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    metadata: Mapping[str, JSONValue] = field(default_factory=_json_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class ModelResult:
    """A provider-neutral model-completion result."""

    output_text: str
    stop_reason: str | None = None
    usage: Mapping[str, int] = field(default_factory=_int_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "usage", MappingProxyType(dict(self.usage)))


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """A transport-neutral tool invocation request."""

    name: str
    arguments: Mapping[str, JSONValue] = field(default_factory=_json_mapping)
    metadata: Mapping[str, JSONValue] = field(default_factory=_json_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", _freeze_mapping(self.arguments))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A transport-neutral tool invocation result."""

    content: Mapping[str, JSONValue] = field(default_factory=_json_mapping)
    is_error: bool = False
    metadata: Mapping[str, JSONValue] = field(default_factory=_json_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", _freeze_mapping(self.content))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class SelectedMemory:
    """One already-selected memory item, frozen at context assembly time."""

    scope: Literal["user", "agent"]
    content: str


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Immutable execution context passed to an agent entrypoint.

    Every field is read-only state the Worker already owns: the frozen effective configuration,
    the rendered context text, the memory items Stage-F already selected, and the narrow model and
    tool ports. Nothing here carries database, credential, Job, Attempt, lease or cancellation
    authority, and the context never triggers retrieval or a memory write of its own.
    """

    run_id: str
    agent_instance_id: str
    configuration: Mapping[str, JSONValue] = field(default_factory=_json_mapping)
    context: str | None = None
    memory: Sequence[SelectedMemory] = ()
    model: ModelPort | None = None
    tools: ToolPort | None = None
    input: Mapping[str, JSONValue] = field(default_factory=_json_mapping)
    metadata: Mapping[str, JSONValue] = field(default_factory=_json_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "configuration", _freeze_mapping(self.configuration))
        object.__setattr__(self, "memory", tuple(self.memory))
        object.__setattr__(self, "input", _freeze_mapping(self.input))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Immutable result returned by an agent entrypoint."""

    output: Mapping[str, JSONValue] = field(default_factory=_json_mapping)
    final_message: str | None = None
    metadata: Mapping[str, JSONValue] = field(default_factory=_json_mapping)

    def __post_init__(self) -> None:
        object.__setattr__(self, "output", _freeze_mapping(self.output))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))
