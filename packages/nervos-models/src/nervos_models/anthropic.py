"""Anthropic provider adapter for the NervOS provider-neutral model port."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import anthropic
from anthropic import AsyncAnthropic
from nervos_core.application.model_completion import (
    MODEL_RESPONSE_INVALID,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
)

PROVIDER_ID = "anthropic"
_USER_ROLE = "user"
_TEXT_BLOCK_TYPE = "text"
_INTERNAL_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})
# Only `end_turn` is a successful natural stop. Every other value, including any raw
# stop reason that merely resembles the canonical name, fails closed as invalid.
_STOP_OUTCOMES: dict[str, StopOutcome] = {
    "end_turn": StopOutcome.STOP,
    "max_tokens": StopOutcome.INCOMPLETE,
    "model_context_window_exceeded": StopOutcome.INCOMPLETE,
    "refusal": StopOutcome.REFUSED,
}

# Most-specific-first mapping of typed SDK failures onto canonical NervOS failure codes.
# No provider message, response body, header, or credential is consulted or propagated.
_EXCEPTION_CODES: tuple[tuple[type[BaseException], str], ...] = (
    (anthropic.AuthenticationError, "model_authentication_failed"),
    (anthropic.PermissionDeniedError, "model_permission_denied"),
    (anthropic.APITimeoutError, "model_timed_out"),
    (anthropic.DeadlineExceededError, "model_timed_out"),
    (anthropic.RateLimitError, "model_rate_limited"),
    (anthropic.APIConnectionError, "model_unavailable"),
    (anthropic.ServiceUnavailableError, "model_unavailable"),
    (anthropic.OverloadedError, "model_unavailable"),
    (anthropic.BadRequestError, "model_request_rejected"),
    (anthropic.RequestTooLargeError, "model_request_rejected"),
    (anthropic.UnprocessableEntityError, "model_request_rejected"),
    (anthropic.ConflictError, "model_request_rejected"),
    (anthropic.NotFoundError, "model_request_rejected"),
    (anthropic.APIResponseValidationError, MODEL_RESPONSE_INVALID),
    (anthropic.APIError, "model_unavailable"),
)


class AnthropicModelCompletion:
    """Translate the NervOS model port onto one non-streaming Anthropic Messages call.

    The adapter receives an already constructed async client, so it performs no credential
    lookup, no import-time network activity, and no retry of its own. It exposes only
    provider-neutral values and never leaks SDK objects, raw bodies, or hidden reasoning.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Issue exactly one bounded non-streaming request and normalize its response."""
        timeout_seconds = request.timeout_ms / 1000
        try:
            message = await self._client.messages.create(
                model=request.model_name,
                max_tokens=request.max_output_tokens,
                system=request.system_instruction,
                messages=[{"role": _USER_ROLE, "content": request.user_text}],
                timeout=timeout_seconds,
            )
        except anthropic.AnthropicError as error:
            raise _normalized_error(error) from error
        try:
            return _normalized_response(message)
        except ModelProviderError as error:
            if error.usage.values() != (None, None, None):
                raise
            raise ModelProviderError(error.code, usage=_usage(message)) from error


def create_anthropic_client(api_key: str, timeout_seconds: float) -> AsyncAnthropic:
    """Build the production async client with provider retries disabled for one-call proof."""
    return AsyncAnthropic(api_key=api_key, max_retries=0, timeout=timeout_seconds)


def _normalized_error(error: anthropic.AnthropicError) -> ModelProviderError:
    for exception_type, code in _EXCEPTION_CODES:
        if isinstance(error, exception_type):
            return ModelProviderError(code)
    if isinstance(error, anthropic.CredentialsError):
        return ModelProviderError("model_authentication_failed")
    # `RetryableError` is a public SDK extension point rather than a credential failure:
    # a transport or middleware may raise it and the SDK re-raises it to the caller. A
    # transient/retryable condition must never be reported as a rejected credential.
    if isinstance(error, anthropic.RetryableError):
        return ModelProviderError("model_unavailable")
    return ModelProviderError(MODEL_RESPONSE_INVALID)


def _normalized_response(message: Any) -> ModelResponse:
    return ModelResponse(
        text=_text_blocks(message),
        model_provider=PROVIDER_ID,
        model_name=_opaque_model_name(message),
        finish_reason=_finish_outcome(message),
        usage=_usage(message),
    )


def _text_blocks(message: Any) -> str:
    """Concatenate only user-visible text blocks; hidden reasoning is never exposed."""
    content_value = getattr(message, "content", None)
    if not isinstance(content_value, Sequence) or isinstance(content_value, str | bytes):
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    content = cast(Sequence[Any], content_value)
    parts: list[str] = []
    text_blocks = 0
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == _TEXT_BLOCK_TYPE:
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                raise ModelProviderError(MODEL_RESPONSE_INVALID)
            parts.append(text)
            text_blocks += 1
        elif block_type in _INTERNAL_BLOCK_TYPES:
            continue
        else:
            raise ModelProviderError(MODEL_RESPONSE_INVALID)
    if text_blocks == 0:
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    return "".join(parts)


def _opaque_model_name(message: Any) -> str:
    name = getattr(message, "model", None)
    if not isinstance(name, str) or not name.strip():
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    return name


def _finish_outcome(message: Any) -> StopOutcome:
    raw = getattr(message, "stop_reason", None)
    if not isinstance(raw, str):
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    outcome = _STOP_OUTCOMES.get(raw)
    if outcome is None:
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    return outcome


def _usage(message: Any) -> ModelUsage | None:
    usage = getattr(message, "usage", None)
    if usage is None:
        return None
    return ModelUsage(
        input_tokens=_token_count(getattr(usage, "input_tokens", None)),
        output_tokens=_token_count(getattr(usage, "output_tokens", None)),
        total_tokens=None,
    )


def _token_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
