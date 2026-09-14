"""OpenAI Responses adapter for the NervOS provider-neutral model port."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import openai
from nervos_core.application.model_completion import (
    INTERNAL_EXECUTION_ERROR,
    MODEL_RESPONSE_INVALID,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
)
from openai import AsyncOpenAI, Omit

PROVIDER_ID = "openai"
# The canonical OpenAI API endpoint, pinned explicitly. The SDK otherwise resolves its base
# URL from the ambient `OPENAI_BASE_URL` variable, which would silently redirect a request
# meant for OpenAI itself to an unrelated OpenAI-compatible endpoint. NervOS publishes the
# canonical `openai` provider only, so the destination is fixed rather than configurable.
_API_BASE_URL = "https://api.openai.com/v1"
# Authentication and scoping are owned explicitly rather than inherited from the process
# environment. See `create_openai_client` for why these headers are declared here.
_AUTHORIZATION_HEADER = "Authorization"
_BEARER_PREFIX = "Bearer "
_ORGANIZATION_HEADER = "OpenAI-Organization"
_PROJECT_HEADER = "OpenAI-Project"
# Header names the locked SDK contributes to its own `default_headers` regardless of the
# process environment: the three HTTP basics, its async marker, and the two scoping headers
# it owns. Read from that public property in the locked version; the telemetry family is
# added at runtime from the public `platform_headers()`. Every *other* name in the SDK's
# custom-header layer can only have been absorbed from the ambient `OPENAI_CUSTOM_HEADERS`
# variable, because that layer is the merge of that variable and `default_headers` alone.
_SDK_OWNED_HEADER_NAMES = frozenset(
    {
        "accept",
        "content-type",
        "user-agent",
        "x-stainless-async",
        "openai-organization",
        "openai-project",
    }
)
_CANONICAL_ACCEPT = "application/json"
_CANONICAL_CONTENT_TYPE = "application/json"
# NervOS' supported ASGI execution path uses asyncio. Freezing this SDK telemetry value also
# prevents the ambient custom-header layer from replacing it before the request is prepared.
_CANONICAL_ASYNC_HEADER = "async:asyncio"
# The names NervOS declares for itself, which are therefore never treated as inherited.
_AUTHORIZED_HEADER_NAMES = frozenset(
    {_AUTHORIZATION_HEADER.lower(), _ORGANIZATION_HEADER.lower(), _PROJECT_HEADER.lower()}
)
_INTERNAL_ITEM_TYPES = frozenset({"reasoning"})
_TOOL_ITEM_TYPES = frozenset(
    {
        "function_call",
        "custom_tool_call",
        "computer_call",
        "web_search_call",
        "file_search_call",
        "mcp_call",
        "mcp_list_tools",
        "mcp_approval_request",
        "shell_call",
        "apply_patch_call",
        "code_interpreter_call",
        "image_generation_call",
        "local_shell_call",
        "tool_search_call",
    }
)
_EXCEPTION_CODES: tuple[tuple[type[BaseException], str], ...] = (
    (openai.AuthenticationError, "model_authentication_failed"),
    (openai.PermissionDeniedError, "model_permission_denied"),
    (openai.RateLimitError, "model_rate_limited"),
    (openai.APITimeoutError, "model_timed_out"),
    (openai.APIConnectionError, "model_unavailable"),
    (openai.InternalServerError, "model_unavailable"),
    (openai.BadRequestError, "model_request_rejected"),
    (openai.NotFoundError, "model_request_rejected"),
    (openai.ConflictError, "model_request_rejected"),
    (openai.UnprocessableEntityError, "model_request_rejected"),
    (openai.APIResponseValidationError, MODEL_RESPONSE_INVALID),
)


class OpenAIModelCompletion:
    """Issue one stateless non-streaming Responses request and normalize it."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            response = await self._client.responses.create(
                model=request.model_name,
                instructions=request.system_instruction,
                input=request.user_text,
                max_output_tokens=request.max_output_tokens,
                store=False,
                background=False,
                stream=False,
                timeout=request.timeout_ms / 1000,
            )
        except openai.OpenAIError as error:
            raise _normalized_error(error) from error
        try:
            return _normalized_response(response, request.model_name)
        except ModelProviderError as error:
            if error.usage.values() != (None, None, None):
                raise
            raise ModelProviderError(error.code, usage=_usage(response)) from error


def create_openai_client(api_key: str, timeout_seconds: float) -> AsyncOpenAI:
    """Build an async client without contacting OpenAI and with retries disabled.

    The client declares its authentication and scoping headers explicitly, because the SDK
    reads provider configuration from the process environment while a client is constructed.
    Client default headers are the SDK's highest-precedence header layer, above both the
    SDK's own credential header and anything parsed from `OPENAI_CUSTOM_HEADERS`. Declaring
    `Authorization` here is therefore what makes the process credential authoritative, and
    what makes the SDK drop an ambient `Authorization` rather than present it. Both
    organization and project are omitted because scoping is not a Stage B feature, so an
    ambient `OPENAI_ORG_ID` or `OPENAI_PROJECT_ID` must not silently narrow a request.

    The same ambient variable can also carry any other, arbitrary header name that the SDK
    has no way to be told about. Those are neutralized afterwards by asking the SDK - through
    its public `default_headers` property - which names it will actually send, and passing
    the SDK's own `Omit` sentinel for every one of them that this factory did not authorize.
    Stage B supports exactly one provider-environment input per provider, its API key; no
    ambient custom-header configuration is part of the supported contract.
    """
    own_headers: dict[str, str | Omit] = {
        _AUTHORIZATION_HEADER: f"{_BEARER_PREFIX}{api_key}",
        _ORGANIZATION_HEADER: Omit(),
        _PROJECT_HEADER: Omit(),
    }
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=_API_BASE_URL,
        max_retries=0,
        timeout=timeout_seconds,
        # `Omit` is the SDK's own header-removal sentinel - its `default_headers` property is
        # typed `dict[str, str | Omit]` and it removes an unset organization/project exactly
        # this way. This constructor annotation is narrower than that runtime contract.
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


def _normalized_error(error: openai.OpenAIError) -> ModelProviderError:
    for exception_type, code in _EXCEPTION_CODES:
        if isinstance(error, exception_type):
            return ModelProviderError(code)
    if isinstance(error, openai.APIStatusError):
        return ModelProviderError(
            "model_unavailable" if error.status_code >= 500 else "model_request_rejected"
        )
    if isinstance(error, openai.APIError):
        return ModelProviderError(INTERNAL_EXECUTION_ERROR)
    return ModelProviderError(INTERNAL_EXECUTION_ERROR)


def _normalized_response(response: Any, request_model_name: str) -> ModelResponse:
    usage = _usage(response)
    if getattr(response, "error", None) is not None:
        raise ModelProviderError("model_unavailable", usage=usage)
    outcome = _outcome(response)
    refused, text = _inspected_output(response)
    if refused:
        raise ModelProviderError("model_refused", usage=usage)
    if outcome is StopOutcome.STOP:
        # Accepted visible text is required only for a completed response. An empty or
        # blank completion is a structural failure rather than a canonical outcome.
        if not text.strip():
            raise ModelProviderError(MODEL_RESPONSE_INVALID, usage=usage)
        return ModelResponse(text, PROVIDER_ID, request_model_name, outcome, usage)
    # A truncated or content-filtered response legitimately carries no output message, so
    # its outcome is still reported. Any partial provider text is discarded rather than
    # exposed, and the returned text is always empty for a non-success outcome.
    return ModelResponse("", PROVIDER_ID, request_model_name, outcome, usage)


def _outcome(response: Any) -> StopOutcome:
    status = getattr(response, "status", None)
    if status == "completed":
        return StopOutcome.STOP
    if status == "failed":
        raise ModelProviderError("model_unavailable")
    if status == "incomplete":
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", None)
        if reason == "max_output_tokens":
            return StopOutcome.INCOMPLETE
        if reason == "content_filter":
            return StopOutcome.REFUSED
    raise ModelProviderError(MODEL_RESPONSE_INVALID)


def _inspected_output(response: Any) -> tuple[bool, str]:
    """Report an explicit refusal plus the accepted visible text.

    The output envelope is validated for every terminal outcome, so a tool, unknown, or
    malformed item still fails the whole response closed even when the status itself is a
    recognized non-success. Only the requirement for accepted visible text is relaxed for
    a non-success outcome, because a truncated or filtered response has no output message
    at all. Precedence is therefore: structured provider error, then an unrecognized
    status, then any unacceptable item, then an explicit refusal, and only last the
    presence or absence of visible text.
    """
    output_value = getattr(response, "output", None)
    if not isinstance(output_value, Sequence) or isinstance(output_value, str | bytes):
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    output = cast(Sequence[Any], output_value)
    messages: list[Any] = []
    for item in output:
        item_type = getattr(item, "type", None)
        if item_type in _INTERNAL_ITEM_TYPES:
            continue
        if item_type in _TOOL_ITEM_TYPES or not isinstance(item_type, str):
            # No tool call is ever executed or accepted, and neither is an item whose
            # type cannot be recognized at all.
            raise ModelProviderError(MODEL_RESPONSE_INVALID)
        if item_type != "message":
            raise ModelProviderError(MODEL_RESPONSE_INVALID)
        messages.append(item)
    if len(messages) > 1:
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    if not messages:
        return False, ""
    if getattr(messages[0], "role", None) != "assistant":
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    content_value = getattr(messages[0], "content", None)
    if not isinstance(content_value, Sequence) or isinstance(content_value, str | bytes):
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    parts: list[str] = []
    refused = False
    for part in cast(Sequence[Any], content_value):
        part_type = getattr(part, "type", None)
        if part_type == "refusal":
            refused = True
        elif part_type == "output_text":
            text = getattr(part, "text", None)
            if not isinstance(text, str):
                raise ModelProviderError(MODEL_RESPONSE_INVALID)
            parts.append(text)
        else:
            raise ModelProviderError(MODEL_RESPONSE_INVALID)
    if refused:
        return True, ""
    return False, "".join(parts)


def _usage(response: Any) -> ModelUsage | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    return ModelUsage(
        input_tokens=_token_count(getattr(usage, "input_tokens", None)),
        output_tokens=_token_count(getattr(usage, "output_tokens", None)),
        total_tokens=_token_count(getattr(usage, "total_tokens", None)),
    )


def _token_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
