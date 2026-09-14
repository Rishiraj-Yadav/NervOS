"""Anthropic provider adapter for the NervOS provider-neutral model port."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import anthropic
from anthropic import AsyncAnthropic, Omit
from nervos_core.application.model_completion import (
    MODEL_RESPONSE_INVALID,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
)

PROVIDER_ID = "anthropic"
# The canonical Anthropic API endpoint, pinned explicitly. The SDK otherwise resolves its
# base URL from the ambient `ANTHROPIC_BASE_URL` variable (and can still be redirected by a
# named CLI profile or credentials file), either of which would silently send a request
# meant for Anthropic somewhere else. NervOS publishes the canonical `anthropic` provider
# only, so the destination is fixed rather than configurable.
_API_BASE_URL = "https://api.anthropic.com"
# Authentication is owned explicitly rather than inherited from the process environment.
# See `create_anthropic_client` for why these headers are declared here.
_API_KEY_HEADER = "x-api-key"
_AUTHORIZATION_HEADER = "Authorization"
# Header names the locked SDK contributes to its own `default_headers` regardless of the
# process environment: the three HTTP basics, its async marker, the API version it pins, and
# the credential names it owns. Read from that public property in the locked version; the
# telemetry family is added at runtime from the public `platform_headers()`. Every *other*
# name in the SDK's custom-header layer can only have been absorbed from the ambient
# `ANTHROPIC_CUSTOM_HEADERS` variable, because that layer is the merge of that variable and
# `default_headers` alone.
_SDK_OWNED_HEADER_NAMES = frozenset(
    {
        "accept",
        "content-type",
        "user-agent",
        "x-stainless-async",
        "anthropic-version",
        "x-api-key",
        "authorization",
    }
)
_CANONICAL_ACCEPT = "application/json"
_CANONICAL_CONTENT_TYPE = "application/json"
_CANONICAL_API_VERSION = "2023-06-01"
# NervOS' supported ASGI execution path uses asyncio. Freezing this SDK telemetry value also
# prevents the ambient custom-header layer from replacing it before the request is prepared.
_CANONICAL_ASYNC_HEADER = "async:asyncio"
# The names NervOS declares for itself, which are therefore never treated as inherited.
_AUTHORIZED_HEADER_NAMES = frozenset({_API_KEY_HEADER, _AUTHORIZATION_HEADER.lower()})
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
    """Build the production async client with provider retries disabled for one-call proof.

    The client declares its authentication header explicitly, for the same reason as the
    OpenAI factory: client default headers outrank the SDK's own credential header and
    anything parsed from `ANTHROPIC_CUSTOM_HEADERS`. Declaring `x-api-key` here is what makes
    the process credential authoritative, and omitting `Authorization` is what stops an
    ambient bearer token from being presented alongside it. Passing an explicit `api_key`
    already prevents the SDK from consulting `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`
    for credentials at all.

    The same ambient variable can also carry any other, arbitrary header name that the SDK
    has no way to be told about. Those are neutralized afterwards by asking the SDK - through
    its public `default_headers` property - which names it will actually send, and passing
    the SDK's own `Omit` sentinel for every one of them that this factory did not authorize.
    Stage B supports exactly one provider-environment input per provider, its API key; no
    ambient custom-header configuration is part of the supported contract.
    """
    own_headers: dict[str, str | Omit] = {
        _API_KEY_HEADER: api_key,
        _AUTHORIZATION_HEADER: Omit(),
    }
    client = AsyncAnthropic(
        api_key=api_key,
        base_url=_API_BASE_URL,
        max_retries=0,
        timeout=timeout_seconds,
        # `Omit` is the SDK's own header-removal sentinel - its header builder accepts a
        # removed-name set and its `default_headers` property is typed `dict[str, str | Omit]`.
        # This constructor annotation is narrower than that runtime contract.
        default_headers=own_headers,  # pyright: ignore[reportArgumentType]
    )
    inherited_headers = _inherited_custom_header_names(client)
    # `set_default_headers` replaces the SDK's custom-header layer outright. Canonical values
    # are frozen explicitly because an ambient header may collide with an SDK-owned name; the
    # client's public user-agent/platform surfaces supply values that must follow the SDK.
    canonical_headers: dict[str, str | Omit] = {
        "Accept": _CANONICAL_ACCEPT,
        "Content-Type": _CANONICAL_CONTENT_TYPE,
        "User-Agent": client.user_agent,
        **client.platform_headers(),
        "X-Stainless-Async": _CANONICAL_ASYNC_HEADER,
        "Anthropic-Version": _CANONICAL_API_VERSION,
        **own_headers,
        **{name: Omit() for name in inherited_headers},
    }
    return client.with_options(
        set_default_headers=canonical_headers,  # pyright: ignore[reportArgumentType]
    )


def _inherited_custom_header_names(client: Any) -> list[str]:
    """Names in the client's custom-header layer that NervOS did not authorize.

    Subtracting the names the SDK owns and the names this factory declares leaves exactly the
    names inherited from the ambient custom-headers variable. This decides by provenance
    rather than by a list of known-bad names, so a name no one anticipated is still removed.
    """
    owned = {name.lower() for name in client.platform_headers()} | _SDK_OWNED_HEADER_NAMES
    return [
        name
        for name in client.default_headers
        if name.lower() not in owned and name.lower() not in _AUTHORIZED_HEADER_NAMES
    ]


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
