"""Narrow provider-neutral model completion boundary and normalized provider outcomes."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from nervos_core.domain.runs import WORKER_RECOVERY_EXHAUSTED, ModelUsage

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


@dataclass(frozen=True, slots=True)
class ModelRequest:
    system_instruction: str
    user_text: str
    model_name: str
    max_output_tokens: int
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    model_provider: str
    model_name: str
    finish_reason: StopOutcome | None = None
    usage: ModelUsage | None = None


class ModelCompletion(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
