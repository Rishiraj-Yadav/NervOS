"""Narrow provider-neutral model completion boundary and normalized provider outcomes."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from nervos_core.domain.runs import ModelUsage

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

_PROVIDER_ERROR_MESSAGES: dict[str, str] = {
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
}


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
    return _PROVIDER_ERROR_MESSAGES[code]


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
