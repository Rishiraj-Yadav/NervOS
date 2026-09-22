"""Narrow provider-neutral model completion boundary and normalized provider outcomes."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from nervos_core.domain.context import HistoricalMessage
from nervos_core.domain.runs import WORKER_RECOVERY_EXHAUSTED, ModelUsage
from nervos_core.domain.tools import JsonValue

# Static NervOS-owned messages. Provider text, bodies, and credentials never appear here.
MODEL_AUTHENTICATION_FAILED = "model_authentication_failed"
MODEL_PERMISSION_DENIED = "model_permission_denied"
MODEL_ACCOUNT_UNAVAILABLE = "model_account_unavailable"
MODEL_RATE_LIMITED = "model_rate_limited"
MODEL_TIMED_OUT = "model_timed_out"
MODEL_UNAVAILABLE = "model_unavailable"
MODEL_REQUEST_REJECTED = "model_request_rejected"
MODEL_RESPONSE_INVALID = "model_response_invalid"
MODEL_OUTPUT_INCOMPLETE = "model_output_incomplete"
MODEL_OUTPUT_TOO_LARGE = "model_output_too_large"
MODEL_REFUSED = "model_refused"
INTERNAL_EXECUTION_ERROR = "internal_execution_error"
# Infrastructural closeout code. It names a durable-execution outcome rather than a provider
# response, and it lives in the same allowlist as every provider code precisely so that there
# is exactly one place that decides which codes may ever be persisted and what they say.
EXECUTION_OUTCOME_AMBIGUOUS = "execution_outcome_ambiguous"
# Infrastructural closeout code for a Run whose owner cancelled it. It is not a provider error,
# a timeout, a retry disposition, or a Worker loss, so it must not be confused with any of them.
# It exists because the frozen `jobs` CHECK requires every terminal Job to carry a non-null
# error pair, and because a cancelled Job must say *why* it ended; the public Run deliberately
# carries no error at all, because cancellation is a terminal lifecycle rather than a failure.
EXECUTION_CANCELLED = "execution_cancelled"
# Stage D loop-limit and tool-outcome codes. They are NervOS loop outcomes rather than provider
# outcomes, and they live in the same allowlist as every provider code so there is still exactly
# one place deciding which codes may be persisted and what they say.
#
# `tool_loop_limit` is the Run reaching a budget the Run itself snapshotted: the model-call budget
# or the tool-call budget, or the consecutive-failure limit. It is decided before any further work,
# so it is DO_NOT_RETRY by class -- replaying would spend the same exhausted budget again.
TOOL_LOOP_LIMIT = "tool_loop_limit"
# A tool call was dispatched and its outcome is genuinely unknown. This is the tool analogue of
# `execution_outcome_ambiguous` and carries the same consequence: no observation that invites the
# model to retry, and no redispatch.
TOOL_OUTCOME_UNKNOWN = "tool_outcome_unknown"
# The assembled catalog could not fit the frozen provider-neutral bounds. It is a configuration
# error, not a provider error: the Run fails before any provider call, and nothing is truncated to
# make it fit.
TOOL_CATALOG_INVALID = "tool_catalog_invalid"
# The one public code a refused tool call carries on the Run timeline. It is deliberately generic
# and deliberately not the durable `permission_decision`: ADR 0015 states a denial reason is an
# internal classification, and D2's reasons (`denied_not_granted`, `denied_connection_disabled`,
# `denied_definition_changed`, and the rest) stay on the invocation row for internal audit rather
# than becoming a public, enumerable description of a user's own configuration.
#
# It covers every refusal that provably did not run: a tool that was never granted, a grant
# withdrawn or drifted between the two checks, a disabled or deleted connection, a definition that
# became unavailable, and the pre-dispatch calls that never became a row at all -- an unknown tool
# name, malformed arguments, and arguments a canonical schema rejected.
TOOL_DENIED = "tool_denied"
# Infrastructural closeout code for a Run that never reached the execution-start boundary and
# whose Job exhausted its claim budget to repeated Worker losses. It is not a provider error, a
# timeout, a cancellation, or a retry disposition: execution provably never began, so this code
# is what licenses the one `failed` Run shape that carries no `started_at`. The code itself is
# owned by the domain, because `Run` validation is what enforces the shape it licenses.

# The single authority for every persistable code and its static NervOS-owned message.
# The database bounds `error_code` only by length, so nothing in SQLite stops a raw exception
# string from being written as a code: the allowlist has to be enforced here, in code.
SAFE_ERROR_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        MODEL_AUTHENTICATION_FAILED: "The model provider rejected its configured credential.",
        MODEL_PERMISSION_DENIED: "The model provider denied access to this request.",
        MODEL_ACCOUNT_UNAVAILABLE: "The model provider account cannot execute this request.",
        MODEL_RATE_LIMITED: "The model provider is temporarily rate limited.",
        MODEL_TIMED_OUT: "The model request exceeded its time limit.",
        MODEL_UNAVAILABLE: "The model provider is temporarily unavailable.",
        MODEL_REQUEST_REJECTED: "The model provider rejected the request configuration.",
        MODEL_RESPONSE_INVALID: "The model provider returned an unusable response.",
        MODEL_OUTPUT_INCOMPLETE: "The model response ended before completion.",
        MODEL_OUTPUT_TOO_LARGE: "The model response exceeded the configured limit.",
        MODEL_REFUSED: "The model could not complete this request.",
        INTERNAL_EXECUTION_ERROR: "The model execution failed safely.",
        EXECUTION_OUTCOME_AMBIGUOUS: (
            "NervOS could not determine whether this run's model request completed, so it was "
            "closed as failed rather than replayed."
        ),
        WORKER_RECOVERY_EXHAUSTED: (
            "NervOS could not start this run after repeated worker losses, so it was closed "
            "without invoking the model."
        ),
        EXECUTION_CANCELLED: (
            "This run was cancelled by its owner and was not executed any further."
        ),
        TOOL_LOOP_LIMIT: (
            "This run reached a configured execution limit before it could be completed."
        ),
        TOOL_OUTCOME_UNKNOWN: (
            "NervOS could not determine whether this run's tool call completed, so it was closed "
            "as failed rather than replayed."
        ),
        TOOL_CATALOG_INVALID: (
            "NervOS could not present this agent's tools within the configured limits, so the run "
            "was closed before it reached the model."
        ),
        TOOL_DENIED: "This tool call was refused before it ran.",
    }
)


class ModelProviderError(Exception):
    """Provider-neutral normalized model execution failure with a safe static message."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        usage: ModelUsage | None = None,
    ) -> None:
        resolved = message if message is not None else provider_error_message(code)
        self._code: str = code
        self._message: str = resolved
        self._usage = usage or ModelUsage()
        super().__init__(resolved)

    @property
    def code(self) -> str:
        """Return the canonical NervOS-owned failure code."""
        return self._code

    @property
    def message(self) -> str:
        """Return the static safe failure message."""
        return self._message

    @property
    def usage(self) -> ModelUsage:
        """Return trustworthy normalized usage available before the failure."""
        return self._usage


def provider_error_message(code: str) -> str:
    """Return the static safe message for a normalized provider failure code."""
    return SAFE_ERROR_MESSAGES[code]


def safe_error_message(code: str) -> str:
    """Return the allowlisted static message, falling back to the internal error's text.

    Anything that is not in the allowlist is refused rather than persisted as itself, so an
    unrecognized exception can never smuggle its own text into durable state.
    """
    return SAFE_ERROR_MESSAGES.get(code, SAFE_ERROR_MESSAGES[INTERNAL_EXECUTION_ERROR])


class ModelAuthenticationError(ModelProviderError):
    """Raised when the provider rejected the configured credential."""

    def __init__(self) -> None:
        super().__init__(MODEL_AUTHENTICATION_FAILED)


class ModelPermissionError(ModelProviderError):
    """Raised when the provider denied access to the request."""

    def __init__(self) -> None:
        super().__init__(MODEL_PERMISSION_DENIED)


class ModelAccountUnavailableError(ModelProviderError):
    """Raised when the provider account cannot execute the request."""

    def __init__(self) -> None:
        super().__init__(MODEL_ACCOUNT_UNAVAILABLE)


class ModelRateLimitedError(ModelProviderError):
    """Raised when the provider is temporarily rate limited."""

    def __init__(self) -> None:
        super().__init__(MODEL_RATE_LIMITED)


class ModelTimeoutError(ModelProviderError):
    """Raised when the bounded provider operation exceeded its time limit."""

    def __init__(self) -> None:
        super().__init__(MODEL_TIMED_OUT)


class ModelUnavailableError(ModelProviderError):
    """Raised when the provider is unreachable or temporarily failing."""

    def __init__(self) -> None:
        super().__init__(MODEL_UNAVAILABLE)


class ModelRequestRejectedError(ModelProviderError):
    """Raised when the provider rejected the request configuration."""

    def __init__(self) -> None:
        super().__init__(MODEL_REQUEST_REJECTED)


class ModelResponseInvalidError(ModelProviderError):
    """Raised when the provider response cannot be normalized safely."""

    def __init__(self, code: str = MODEL_RESPONSE_INVALID) -> None:
        super().__init__(code)


class ModelIncompleteOutputError(ModelProviderError):
    """Raised when the provider response ended before completion."""

    def __init__(self) -> None:
        super().__init__(MODEL_OUTPUT_INCOMPLETE)


class ModelOutputTooLargeError(ModelProviderError):
    """Raised when the provider response exceeded the Run output limits."""

    def __init__(self) -> None:
        super().__init__(MODEL_OUTPUT_TOO_LARGE)


class ModelRefusedError(ModelProviderError):
    """Raised when the provider declined to complete the request."""

    def __init__(self) -> None:
        super().__init__(MODEL_REFUSED)


class StopOutcome(StrEnum):
    """Normalized provider termination categories for one non-streaming completion."""

    STOP = "stop"
    INCOMPLETE = "incomplete"
    REFUSED = "refused"
    INVALID = "invalid"
    # The provider stopped because it wants a tool run. It is a normalized termination like any
    # other, not a tool itself: nothing about it reaches an execution path.
    TOOL_USE = "tool_use"


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """One tool offered to a provider: the neutral projection of a durable Tool Descriptor.

    It carries the durable `model_name` rather than any upstream name, because the name the model
    is offered is the name it will call back with. It knows nothing about descriptors, grants, or
    persistence -- a schema is what a provider needs, and nothing more.
    """

    name: str
    description: str
    input_schema: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool call a model asked for, in the one canonical form every adapter produces.

    Arguments are carried as the provider's own JSON **text**, never as a Python object: adapters
    differ in whether they hand back a decoded value or a string, and normalizing both to text here
    is what keeps a provider-shaped object from ever escaping `nervos-models`. The text is parsed
    once, later, by the loop -- which is also the only place it is authorized.
    """

    call_id: str
    name: str
    arguments_json: str


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    """One accepted assistant turn, preserved exactly as the provider returned it.

    `text` and `tool_calls` are both kept. A turn may legitimately carry prose alongside tool
    calls, and that prose is part of the conversation the provider will be asked to continue from:
    dropping it would silently rewrite history that the model can see on the wire.
    """

    text: str
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolResultTurn:
    """One tool's result, addressed back to the call that requested it.

    `structured` is the tool's own JSON-safe output, kept alongside its text so a provider that
    understands structured content receives it rather than a re-serialized approximation.
    """

    call_id: str
    text: str
    structured: JsonValue | None = None
    is_error: bool = False


type ConversationTurn = AssistantTurn | ToolResultTurn


@dataclass(frozen=True, slots=True)
class ModelRequest:
    system_instruction: str
    user_text: str
    model_name: str
    max_output_tokens: int
    timeout_ms: int
    # Both default to empty, so a tool-free request is exactly the Stage B/C request it always was.
    tools: tuple[ToolSchema, ...] = ()
    turns: tuple[ConversationTurn, ...] = ()
    history: tuple[HistoricalMessage, ...] = ()
    compaction_context: str | None = None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    model_provider: str
    model_name: str
    finish_reason: StopOutcome | None = None
    usage: ModelUsage | None = None
    tool_calls: tuple[ToolCall, ...] = ()


class ModelCompletion(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
