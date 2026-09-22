"""Anthropic provider adapter for the NervOS provider-neutral model port."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

import anthropic
from anthropic import AsyncAnthropic, Omit
from nervos_core.application.model_completion import (
    MODEL_RESPONSE_INVALID,
    AssistantTurn,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
    ToolCall,
    ToolResultTurn,
)
from nervos_core.domain.conversations import MessageRole
from nervos_core.domain.tools import JsonValue, canonical_json_text

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
_ASSISTANT_ROLE = "assistant"
_TEXT_BLOCK_TYPE = "text"
_TOOL_USE_BLOCK_TYPE = "tool_use"
_TOOL_RESULT_BLOCK_TYPE = "tool_result"
_INTERNAL_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})
# Only `end_turn` is a successful natural stop. Every other value, including any raw
# stop reason that merely resembles the canonical name, fails closed as invalid.
_STOP_OUTCOMES: dict[str, StopOutcome] = {
    "end_turn": StopOutcome.STOP,
    "max_tokens": StopOutcome.INCOMPLETE,
    "model_context_window_exceeded": StopOutcome.INCOMPLETE,
    "refusal": StopOutcome.REFUSED,
    # A tool stop is a normalized termination, not an execution: it says the model wants a tool
    # run, and nothing about it reaches a tool.
    "tool_use": StopOutcome.TOOL_USE,
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
        request_kwargs: dict[str, Any] = {
            "model": request.model_name,
            "max_tokens": request.max_output_tokens,
            "system": request.system_instruction,
            "messages": _messages(request),
            "timeout": timeout_seconds,
        }
        if request.tools:
            request_kwargs["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": dict(tool.input_schema),
                }
                for tool in request.tools
            ]
            # No parallelism: one call at a time, in the order the model produced them, so the
            # loop's ambiguity boundary and its budgets stay one-dimensional.
            request_kwargs["tool_choice"] = {
                "type": "auto",
                "disable_parallel_tool_use": True,
            }
        try:
            message = await self._client.messages.create(**request_kwargs)
        except anthropic.AnthropicError as error:
            raise _normalized_error(error) from error
        try:
            return _normalized_response(message, tools_enabled=bool(request.tools))
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


def _normalized_response(message: Any, *, tools_enabled: bool) -> ModelResponse:
    text, tool_calls = _content_blocks(message, tools_enabled=tools_enabled)
    outcome = _finish_outcome(message)
    # A model that was offered no tools cannot be asking for one, so a tool termination here is an
    # unusable response rather than a new outcome. This is what keeps the tool-free wire path
    # exactly as strict as it was before D4 existed.
    if outcome is StopOutcome.TOOL_USE and not tools_enabled:
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    return ModelResponse(
        text=text,
        model_provider=PROVIDER_ID,
        model_name=_opaque_model_name(message),
        finish_reason=outcome,
        usage=_usage(message),
        tool_calls=tool_calls,
    )


def _messages(request: ModelRequest) -> list[dict[str, Any]]:
    """Build the wire message list: multi-turn history, the current user request, and tool turns.

    Consecutive tool results are gathered into one user message, which is the shape the Messages
    API uses for tool results, and a pending run is flushed **before** the next assistant turn --
    otherwise a later round's results would be appended after the turn they answer. Nothing is
    dropped: an assistant turn's prose is carried beside its tool uses, because the model will be
    asked to continue from exactly this conversation.
    """
    messages: list[dict[str, Any]] = []

    compaction_prefix = (
        f"[Earlier Conversation Context]\n{request.compaction_context}"
        if request.compaction_context
        else None
    )

    if request.history:
        for idx, hist in enumerate(request.history):
            if hist.role == MessageRole.USER:
                content = hist.content
                if idx == 0 and compaction_prefix is not None:
                    content = f"{compaction_prefix}\n\n{content}"
                    compaction_prefix = None
                messages.append({"role": _USER_ROLE, "content": content})
            else:
                messages.append(
                    {
                        "role": _ASSISTANT_ROLE,
                        "content": [{"type": _TEXT_BLOCK_TYPE, "text": hist.content}],
                    }
                )

    current_user_content = request.user_text
    if compaction_prefix is not None:
        current_user_content = f"{compaction_prefix}\n\n{current_user_content}"
    messages.append({"role": _USER_ROLE, "content": current_user_content})

    pending: list[dict[str, Any]] = []
    for turn in request.turns:
        if isinstance(turn, AssistantTurn):
            if pending:
                messages.append({"role": _USER_ROLE, "content": pending})
                pending = []
            messages.append({"role": _ASSISTANT_ROLE, "content": _assistant_content(turn)})
            continue
        pending.append(_tool_result_block(turn))
    # A trailing run of results is only flushed at the end, so the loop back refers to them.
    if pending:
        messages.append({"role": _USER_ROLE, "content": pending})
    return messages


def _assistant_content(turn: AssistantTurn) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if turn.text:
        blocks.append({"type": _TEXT_BLOCK_TYPE, "text": turn.text})
    for call in turn.tool_calls:
        blocks.append(
            {
                "type": _TOOL_USE_BLOCK_TYPE,
                "id": call.call_id,
                "name": call.name,
                # The model produced this payload as JSON text; it is echoed verbatim rather than
                # re-decoded here, so no provider-shaped object is reconstructed on the way out.
                "input": _decode_arguments(call.arguments_json),
            }
        )
    return blocks


def _tool_result_block(turn: ToolResultTurn) -> dict[str, Any]:
    """Render one tool result, keeping structured content rather than flattening it away.

    The wire field is a list of content blocks, so both members of the observation are preserved:
    the tool's text, and -- when it has one -- its canonical JSON. Nothing invents a provider-side
    content type, and nothing omits a part the tool actually produced.
    """
    content: list[dict[str, Any]] = [{"type": _TEXT_BLOCK_TYPE, "text": turn.text}]
    if turn.structured is not None:
        content.append(
            {
                "type": _TEXT_BLOCK_TYPE,
                "text": canonical_json_text(cast("JsonValue", turn.structured)),
            }
        )
    block: dict[str, Any] = {
        "type": _TOOL_RESULT_BLOCK_TYPE,
        "tool_use_id": turn.call_id,
        "content": content,
    }
    if turn.is_error:
        block["is_error"] = True
    return block


def _decode_arguments(arguments_json: str) -> Any:
    """Decode an argument payload for the wire, or fail closed as an unusable response."""
    try:
        return json.loads(arguments_json)
    except (json.JSONDecodeError, ValueError, RecursionError) as error:
        raise ModelProviderError(MODEL_RESPONSE_INVALID) from error


def _content_blocks(message: Any, *, tools_enabled: bool) -> tuple[str, tuple[ToolCall, ...]]:
    """Return only user-visible text plus locally supported tool uses.

    Hidden reasoning is never exposed. A `tool_use` block is accepted only when this request
    actually offered tools; without them a tool use is an unusable response rather than something
    to run, which is what keeps the tool-free wire path exactly as strict as it was before D4.
    Every other block type still fails closed.
    """
    content_value = getattr(message, "content", None)
    if not isinstance(content_value, Sequence) or isinstance(content_value, str | bytes):
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    content = cast(Sequence[Any], content_value)
    parts: list[str] = []
    calls: list[ToolCall] = []
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
        elif block_type == _TOOL_USE_BLOCK_TYPE and tools_enabled:
            calls.append(_tool_call(block))
        else:
            raise ModelProviderError(MODEL_RESPONSE_INVALID)
    # Text is required only when the turn carries no tool calls: a model may legitimately answer a
    # tool turn with nothing but the calls, and blank text is never what decides that.
    if text_blocks == 0 and not calls:
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    return "".join(parts), tuple(calls)


def _tool_call(block: Any) -> ToolCall:
    call_id = getattr(block, "id", None)
    name = getattr(block, "name", None)
    if not isinstance(call_id, str) or not isinstance(name, str):
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    payload = getattr(block, "input", None)
    if not isinstance(payload, dict):
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    try:
        # Normalize to the one canonical text form. The payload is not schema-authorized here:
        # authorizing it belongs to the loop, and this adapter has no tool schema to authorize
        # against.
        arguments_json = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise ModelProviderError(MODEL_RESPONSE_INVALID) from error
    return ToolCall(call_id=call_id, name=name, arguments_json=arguments_json)


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
