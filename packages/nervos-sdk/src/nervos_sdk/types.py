"""Transport-neutral public data contracts for NervOS agents."""

from __future__ import annotations

import json
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
    item_id: int | None = None
    version: int | None = None


@dataclass(frozen=True, slots=True)
class MemoryProposal:
    """A bounded suggestion; the owner policy, not this object, authorizes retention."""

    content: str
    scope: Literal["agent", "user"] = "agent"

    def __post_init__(self) -> None:
        if not self.content.strip() or "\x00" in self.content or len(self.content.encode()) > 2000:
            raise ValueError("memory proposal must contain 1-2000 UTF-8 bytes")
        if self.scope not in ("agent", "user"):
            raise ValueError("invalid memory proposal scope")


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
    # ``None`` for every ordinary Run, so a pre-existing package that never reads it is
    # unaffected. When present, this step *is* a durable workflow step and its result must
    # carry a ``WorkflowResult`` back.
    workflow: WorkflowSnapshot | None = None

    def model_request(self, system_instruction: str, *, model: str | None = None) -> ModelRequest:
        """Build a request without copying snapshot history or memory into the current input.

        With structured-v1 the Worker injects the frozen selected context exactly once.
        Legacy packages continue to use their supplied text as before.
        """
        text = self.input.get("text", "")
        if not isinstance(text, str):
            raise ValueError("agent input text is missing")
        return ModelRequest(
            messages=(ModelMessage("system", system_instruction), ModelMessage("user", text)),
            model=model,
        )

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
    memory_proposals: Sequence[MemoryProposal] = ()
    workflow: WorkflowResult | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "output", _freeze_mapping(self.output))
        proposals = tuple(self.memory_proposals)
        if len(proposals) > 6 or sum(len(p.content.encode()) for p in proposals) > 8000:
            raise ValueError("memory proposals exceed the batch budget")
        object.__setattr__(self, "memory_proposals", proposals)
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))


# ---------------------------------------------------------------------------------------
# Durable workflow contracts (ADR 0039, host capability ``workflow-v1``)
# ---------------------------------------------------------------------------------------

WORKFLOW_SNAPSHOT_STATE_VERSION = 1
CHECKPOINT_MAX_BYTES = 64 * 1024
SUMMARY_MAX_CHARS = 512
WAIT_SECONDS_MAX = 7 * 24 * 60 * 60
DECISION_EXPIRY_MAX_SECONDS = 7 * 24 * 60 * 60

DIRECTIVE_KINDS = frozenset({"complete", "next", "wait"})
WAIT_KINDS = frozenset({"time", "signal", "owner_decision"})
_SIGNAL_KEY_MAX = 64


def _canonical_size(value: JSONValue) -> int:
    """Canonical UTF-8 size, matching the Worker-side bound rather than approximating it."""
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_mapping_value,
            allow_nan=False,
        ).encode("utf-8")
    )


def _json_mapping_value(value: object) -> dict[str, JSONValue]:
    if isinstance(value, Mapping):
        return dict(cast(Mapping[str, JSONValue], value))
    raise TypeError("Workflow state must be JSON data")


@dataclass(frozen=True, slots=True)
class WorkflowDecisionProposal:
    """One exact external action a step asks an owner to authorize.

    This is *proposed application work*, never dispatch authority. The Worker binds the
    checkpoint revision, the canonical argument digest, and the pinned package and
    configuration identity itself: a package cannot widen what it proposed, and editing any
    bound value invalidates the decision rather than silently reusing it (ADR 0039 §W3).
    """

    tool_definition_id: int
    upstream_name: str
    arguments: Mapping[str, JSONValue] = field(default_factory=_json_mapping)
    preview: Mapping[str, JSONValue] = field(default_factory=_json_mapping)
    expires_in_seconds: int = 900

    def __post_init__(self) -> None:
        if self.tool_definition_id <= 0:
            raise ValueError("a decision proposal requires a tool definition")
        if not self.upstream_name or len(self.upstream_name) > 128 or "\x00" in self.upstream_name:
            raise ValueError("decision upstream name is invalid")
        if not 60 <= self.expires_in_seconds <= DECISION_EXPIRY_MAX_SECONDS:
            raise ValueError("decision expiry is outside its bounds")
        if _canonical_size(self.arguments) > CHECKPOINT_MAX_BYTES:
            raise ValueError("decision arguments exceed the checkpoint bound")
        object.__setattr__(self, "arguments", _freeze_mapping(self.arguments))
        object.__setattr__(self, "preview", _freeze_mapping(self.preview))


@dataclass(frozen=True, slots=True)
class WorkflowDirective:
    """How the host should advance after this step. Exactly one legal shape per kind.

    ``complete`` and ``next`` carry nothing else. ``wait`` carries exactly the one field its
    wait kind names, which is what stops a package from asking to be woken by a clock *and*
    an owner decision in the same step and leaving the intent ambiguous on a restart.
    """

    kind: Literal["complete", "next", "wait"]
    wait_kind: Literal["time", "signal", "owner_decision"] | None = None
    wait_seconds: int | None = None
    signal_key: str | None = None
    decision: WorkflowDecisionProposal | None = None

    def __post_init__(self) -> None:
        if self.kind not in DIRECTIVE_KINDS:
            raise ValueError("workflow directive kind is not supported")
        if self.kind != "wait":
            if (self.wait_kind, self.wait_seconds, self.signal_key, self.decision) != (
                None,
                None,
                None,
                None,
            ):
                raise ValueError("only a wait directive carries a wait condition")
            return
        if self.wait_kind not in WAIT_KINDS:
            raise ValueError("workflow wait kind is not supported")
        present = (
            self.wait_seconds is not None,
            self.signal_key is not None,
            self.decision is not None,
        )
        if present.count(True) != 1:
            raise ValueError("a wait directive carries exactly one wait condition")
        if self.wait_kind == "time":
            if self.wait_seconds is None or not 1 <= self.wait_seconds <= WAIT_SECONDS_MAX:
                raise ValueError(f"a time wait requires 1..{WAIT_SECONDS_MAX} seconds")
        elif self.wait_kind == "signal":
            key = self.signal_key
            if key is None or not 1 <= len(key) <= _SIGNAL_KEY_MAX or not key.isascii():
                raise ValueError("a signal wait requires a bounded ASCII signal key")
        elif self.decision is None:
            raise ValueError("a decision wait requires a decision proposal")


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    """The durable checkpoint this step resumes from, frozen at dispatch.

    A package reads this state and returns a whole new one; it cannot patch state in place,
    because the Worker keeps the previous revision until the step commits against its fence.
    """

    step_number: int
    checkpoint_revision: int
    state_version: int = WORKFLOW_SNAPSHOT_STATE_VERSION
    state: Mapping[str, JSONValue] = field(default_factory=_json_mapping)
    wake_signal: Mapping[str, JSONValue] = field(default_factory=_json_mapping)

    def __post_init__(self) -> None:
        if self.step_number < 1 or self.checkpoint_revision < 0:
            raise ValueError("workflow snapshot counters are out of bounds")
        if self.state_version != WORKFLOW_SNAPSHOT_STATE_VERSION:
            raise ValueError("unsupported workflow state version")
        if _canonical_size(self.state) > CHECKPOINT_MAX_BYTES:
            raise ValueError("workflow checkpoint exceeds the checkpoint bound")
        object.__setattr__(self, "state", _freeze_mapping(self.state))
        if _canonical_size(self.wake_signal) > 8 * 1024 + 256:
            raise ValueError("workflow wake signal exceeds the signal bound")
        object.__setattr__(self, "wake_signal", _freeze_mapping(self.wake_signal))


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    """One step's bounded outcome, returned to the host for fenced commit."""

    directive: WorkflowDirective
    state: Mapping[str, JSONValue] = field(default_factory=_json_mapping)
    summary: str | None = None

    def __post_init__(self) -> None:
        if self.summary is not None and (
            not self.summary.strip()
            or "\x00" in self.summary
            or len(self.summary) > SUMMARY_MAX_CHARS
        ):
            raise ValueError("workflow step summary is out of bounds")
        if _canonical_size(self.state) > CHECKPOINT_MAX_BYTES:
            raise ValueError("workflow checkpoint exceeds the checkpoint bound")
        object.__setattr__(self, "state", _freeze_mapping(self.state))
