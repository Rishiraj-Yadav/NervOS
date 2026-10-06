"""Gemini Developer API adapter for the provider-neutral NervOS model port."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from typing import Any, cast

import google.genai.errors as genai_errors
import httpx
from google import genai
from google.genai import types
from nervos_core.application.model_completion import (
    MODEL_RESPONSE_INVALID,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
    ToolCall,
    ToolResultTurn,
    ToolSchema,
)
from nervos_core.domain.conversations import MessageRole
from nervos_core.domain.tools import JsonValue, canonical_json_text, validate_json_value

PROVIDER_ID = "gemini"
_API_BASE_URL = "https://generativelanguage.googleapis.com"
_API_VERSION = "v1beta"
_MAX_PENDING_SIGNATURES = 2048
_UNSUPPORTED_PART_FIELDS = (
    "media_resolution",
    "code_execution_result",
    "executable_code",
    "file_data",
    "function_response",
    "inline_data",
    "video_metadata",
    "tool_call",
    "tool_response",
)
_STOP_REASONS: dict[str, StopOutcome] = {
    "STOP": StopOutcome.STOP,
    "MAX_TOKENS": StopOutcome.INCOMPLETE,
    "SAFETY": StopOutcome.REFUSED,
    "RECITATION": StopOutcome.REFUSED,
    "LANGUAGE": StopOutcome.REFUSED,
    "BLOCKLIST": StopOutcome.REFUSED,
    "PROHIBITED_CONTENT": StopOutcome.REFUSED,
    "SPII": StopOutcome.REFUSED,
    "IMAGE_SAFETY": StopOutcome.REFUSED,
    "IMAGE_PROHIBITED_CONTENT": StopOutcome.REFUSED,
    "IMAGE_RECITATION": StopOutcome.REFUSED,
    "IMAGE_OTHER": StopOutcome.REFUSED,
    "MALFORMED_FUNCTION_CALL": StopOutcome.INVALID,
    "UNEXPECTED_TOOL_CALL": StopOutcome.INVALID,
    "NO_IMAGE": StopOutcome.INVALID,
    "OTHER": StopOutcome.INVALID,
}


class GeminiModelCompletion:
    """Translate one bounded GenerateContent call and its manual function turns."""

    def __init__(self, client: genai.Client) -> None:
        self._client = client
        # GenerateContent is stateless. The neutral ToolCall contract intentionally contains no
        # Google objects, so opaque thought signatures are held only across this adapter's
        # immediate ToolLoop continuation and keyed by the neutral call correlation ID.
        self._signatures: OrderedDict[
            str, tuple[str | None, bytes | None, str, tuple[types.Part, ...]]
        ] = OrderedDict()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        contents = _contents(request, self._signatures)
        config = types.GenerateContentConfig(
            system_instruction=request.system_instruction,
            max_output_tokens=request.max_output_tokens,
            temperature=request.temperature,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            http_options=types.HttpOptions(
                base_url=_API_BASE_URL,
                api_version=_API_VERSION,
                timeout=request.timeout_ms,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        if request.tools:
            declarations = [_function_declaration(tool) for tool in request.tools]
            config.tools = [types.Tool(function_declarations=declarations)]
            config.tool_config = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.AUTO
                )
            )
        try:
            models: Any = self._client.aio.models
            response = await models.generate_content(
                model=request.model_name,
                contents=contents,
                config=config,
            )
        except asyncio.CancelledError:
            raise
        except genai_errors.APIError as error:
            raise _normalized_api_error(error) from error
        except (httpx.TimeoutException, TimeoutError) as error:
            raise ModelProviderError("model_timed_out") from error
        except httpx.RequestError as error:
            raise ModelProviderError("model_unavailable") from error

        _release_signatures(request.turns, self._signatures)
        return _normalized_response(response, request, self._signatures)


def create_gemini_client(api_key: str, timeout_seconds: float) -> genai.Client:
    """Create a Developer API client with explicit auth, endpoint, timeout, and no retries.

    `api_key`, `enterprise=False`, and a pinned `HttpOptions.base_url` override the SDK's
    corresponding ambient credential/backend/endpoint selection. The SDK's supported custom
    base URL environment variable (`GOOGLE_GEMINI_BASE_URL`) is therefore not authoritative.
    The explicit Developer API selection also makes ADC and Vertex project/location settings
    inapplicable. The client owns the async HTTP resources and is closed by Worker composition.
    """
    return genai.Client(
        api_key=api_key,
        enterprise=False,
        vertexai=False,
        credentials=None,
        project=None,
        location=None,
        http_options=types.HttpOptions(
            base_url=_API_BASE_URL,
            api_version=_API_VERSION,
            timeout=int(timeout_seconds * 1000),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def _contents(
    request: ModelRequest,
    signatures: OrderedDict[str, tuple[str | None, bytes | None, str, tuple[types.Part, ...]]],
) -> list[types.Content]:
    """Map history, current input, assistant function calls, and tool results faithfully."""
    prefix = [
        value
        for value in (
            request.user_memory_context,
            request.agent_memory_context,
            f"[Earlier Conversation Context]\n{request.compaction_context}"
            if request.compaction_context
            else None,
        )
        if value
    ]
    prefix_text = "\n\n".join(prefix)
    result: list[types.Content] = []
    if request.history:
        for index, historical in enumerate(request.history):
            text = historical.content
            if index == 0 and historical.role is MessageRole.USER and prefix_text:
                text = f"{prefix_text}\n\n{text}"
                prefix_text = ""
            result.append(
                types.Content(
                    role="user" if historical.role is MessageRole.USER else "model",
                    parts=[types.Part(text=text)],
                )
            )
    user_text = request.user_text
    if prefix_text:
        user_text = f"{prefix_text}\n\n{user_text}"
    result.append(types.Content(role="user", parts=[types.Part(text=user_text)]))

    pending_responses: list[types.Part] = []
    for turn in request.turns:
        if isinstance(turn, ToolResultTurn):
            provider_call_id, _signature, function_name, _thoughts = signatures.get(
                turn.call_id, (turn.call_id, None, "", ())
            )
            if not function_name:
                raise ModelProviderError(MODEL_RESPONSE_INVALID)
            payload: dict[str, JsonValue]
            if turn.is_error:
                payload = {"error": turn.text}
            elif turn.structured is not None and isinstance(turn.structured, dict):
                payload = cast(dict[str, JsonValue], turn.structured)
            else:
                payload = {"result": turn.text}
            pending_responses.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        id=provider_call_id,
                        name=function_name,
                        response=payload,
                    )
                )
            )
            continue

        if pending_responses:
            result.append(types.Content(role="user", parts=pending_responses))
            pending_responses = []
        parts: list[types.Part] = []
        if turn.text:
            parts.append(types.Part(text=turn.text))
        for call in turn.tool_calls:
            provider_call_id, signature, _function_name, thoughts = signatures.get(
                call.call_id, (call.call_id, None, call.name, ())
            )
            parts.extend(part.model_copy(deep=True) for part in thoughts)
            arguments = _decode_arguments(call.arguments_json)
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(
                        id=provider_call_id,
                        name=call.name,
                        args=arguments,
                    ),
                    thought_signature=signature,
                )
            )
        if not parts:
            raise ModelProviderError(MODEL_RESPONSE_INVALID)
        result.append(types.Content(role="model", parts=parts))
    if pending_responses:
        result.append(types.Content(role="user", parts=pending_responses))
    return result


def _function_declaration(tool: ToolSchema) -> types.FunctionDeclaration:
    """Pass the already-admitted canonical JSON Schema without weakening its constraints."""
    return types.FunctionDeclaration(
        name=tool.name,
        description=tool.description,
        parameters_json_schema=dict(tool.input_schema),
    )


def _decode_arguments(arguments_json: str) -> dict[str, Any]:
    try:
        value = json.loads(arguments_json)
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as error:
        raise ModelProviderError(MODEL_RESPONSE_INVALID) from error
    if (
        not isinstance(value, dict)
        or validate_json_value(cast(dict[str, JsonValue], value)) is not None
    ):
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    return cast(dict[str, Any], value)


def _normalized_response(
    response: Any,
    request: ModelRequest,
    signatures: OrderedDict[str, tuple[str | None, bytes | None, str, tuple[types.Part, ...]]],
) -> ModelResponse:
    usage = _usage(getattr(response, "usage_metadata", None))
    prompt_feedback = getattr(response, "prompt_feedback", None)
    block_reason = getattr(prompt_feedback, "block_reason", None)
    if block_reason not in (None, "BLOCK_REASON_UNSPECIFIED", "UNSPECIFIED"):
        raise ModelProviderError("model_refused", usage=usage)

    candidate_value = getattr(response, "candidates", None)
    if not isinstance(candidate_value, list):
        raise ModelProviderError(MODEL_RESPONSE_INVALID, usage=usage)
    candidates = cast(list[Any], candidate_value)
    if len(candidates) != 1:
        raise ModelProviderError(MODEL_RESPONSE_INVALID, usage=usage)
    candidate = cast(types.Candidate, candidates[0])
    finish_reason = _finish_reason(getattr(candidate, "finish_reason", None))
    content = getattr(candidate, "content", None)
    if getattr(content, "role", None) != "model":
        raise ModelProviderError(MODEL_RESPONSE_INVALID, usage=usage)
    parts_value = getattr(content, "parts", None)
    if not isinstance(parts_value, list):
        if finish_reason is StopOutcome.REFUSED:
            return ModelResponse("", PROVIDER_ID, request.model_name, finish_reason, usage)
        raise ModelProviderError(MODEL_RESPONSE_INVALID, usage=usage)
    parts = cast(list[Any], parts_value)

    text_parts: list[str] = []
    calls: list[ToolCall] = []
    staged_signatures: list[
        tuple[str, tuple[str | None, bytes | None, str, tuple[types.Part, ...]]]
    ] = []
    pending_thoughts: list[types.Part] = []
    for index, part in enumerate(parts):
        if not isinstance(part, types.Part):
            raise ModelProviderError(MODEL_RESPONSE_INVALID, usage=usage)
        if any(getattr(part, field) is not None for field in _UNSUPPORTED_PART_FIELDS):
            raise ModelProviderError(MODEL_RESPONSE_INVALID, usage=usage)
        if part.thought and part.function_call is None and part.text is not None:
            pending_thoughts.append(part.model_copy(deep=True))
            continue
        if part.text is not None:
            if part.function_call is not None:
                raise ModelProviderError(MODEL_RESPONSE_INVALID, usage=usage)
            text_parts.append(part.text)
            continue
        if part.function_call is not None:
            if not request.tools:
                raise ModelProviderError(MODEL_RESPONSE_INVALID, usage=usage)
            call = part.function_call
            name = call.name
            args = call.args
            if not isinstance(name, str) or not name or not isinstance(args, dict):
                raise ModelProviderError(MODEL_RESPONSE_INVALID, usage=usage)
            try:
                if validate_json_value(cast(dict[str, JsonValue], args)) is not None:
                    raise ValueError
                arguments_json = canonical_json_text(cast(JsonValue, args))
            except (TypeError, ValueError, RecursionError) as error:
                raise ModelProviderError(MODEL_RESPONSE_INVALID, usage=usage) from error
            provider_id = call.id or None
            signature = part.thought_signature
            correlation_id = provider_id or _generated_call_id(
                response, index, name, arguments_json
            )
            if correlation_id in signatures or any(
                existing_id == correlation_id for existing_id, _ in staged_signatures
            ):
                raise ModelProviderError(MODEL_RESPONSE_INVALID, usage=usage)
            staged_signatures.append(
                (correlation_id, (provider_id, signature, name, tuple(pending_thoughts)))
            )
            pending_thoughts = []
            calls.append(ToolCall(correlation_id, name, arguments_json))
            continue
        # Image/audio/video, hosted-tool results, inline data, and future part types are not
        # accepted by the text-and-manual-functions NervOS contract.
        raise ModelProviderError(MODEL_RESPONSE_INVALID, usage=usage)

    if finish_reason is StopOutcome.REFUSED:
        raise ModelProviderError("model_refused", usage=usage)
    if finish_reason is StopOutcome.INVALID:
        raise ModelProviderError(MODEL_RESPONSE_INVALID, usage=usage)
    if finish_reason is StopOutcome.INCOMPLETE:
        return ModelResponse("", PROVIDER_ID, request.model_name, finish_reason, usage)
    if calls:
        for correlation_id, signature_entry in staged_signatures:
            signatures[correlation_id] = signature_entry
            signatures.move_to_end(correlation_id)
        while len(signatures) > _MAX_PENDING_SIGNATURES:
            signatures.popitem(last=False)
        return ModelResponse(
            "".join(text_parts),
            PROVIDER_ID,
            request.model_name,
            StopOutcome.TOOL_USE,
            usage,
            tuple(calls),
        )
    text = "".join(text_parts)
    if not text.strip():
        raise ModelProviderError(MODEL_RESPONSE_INVALID, usage=usage)
    return ModelResponse(text, PROVIDER_ID, request.model_name, StopOutcome.STOP, usage)


def _generated_call_id(response: Any, index: int, name: str, arguments_json: str) -> str:
    response_id = getattr(response, "response_id", None)
    material = f"{response_id or ''}\0{index}\0{name}\0{arguments_json}"
    return "gemini-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:48]


def _release_signatures(
    turns: tuple[Any, ...],
    signatures: OrderedDict[str, tuple[str | None, bytes | None, str, tuple[types.Part, ...]]],
) -> None:
    for turn in turns:
        if isinstance(turn, ToolResultTurn):
            signatures.pop(turn.call_id, None)


def _finish_reason(raw: Any) -> StopOutcome:
    value = getattr(raw, "value", raw)
    if not isinstance(value, str):
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    outcome = _STOP_REASONS.get(value)
    if outcome is None:
        raise ModelProviderError(MODEL_RESPONSE_INVALID)
    return outcome


def _usage(raw: Any) -> ModelUsage | None:
    if raw is None:
        return None
    return ModelUsage(
        input_tokens=_token_count(getattr(raw, "prompt_token_count", None)),
        output_tokens=_token_count(getattr(raw, "candidates_token_count", None)),
        total_tokens=_token_count(getattr(raw, "total_token_count", None)),
    )


def _token_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _normalized_api_error(error: genai_errors.APIError) -> ModelProviderError:
    code = getattr(error, "code", None)
    if code in {401}:
        return ModelProviderError("model_authentication_failed")
    if code in {403}:
        return ModelProviderError("model_permission_denied")
    if code in {408, 504}:
        return ModelProviderError("model_timed_out")
    if code == 429:
        return ModelProviderError("model_rate_limited")
    if code in {400, 404, 409, 422}:
        return ModelProviderError("model_request_rejected")
    if isinstance(error, genai_errors.ServerError) or (isinstance(code, int) and code >= 500):
        return ModelProviderError("model_unavailable")
    if isinstance(code, int) and 400 <= code < 500:
        return ModelProviderError("model_request_rejected")
    return ModelProviderError("model_unavailable")
