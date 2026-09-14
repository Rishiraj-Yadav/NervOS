"""Deterministic Anthropic adapter contract tests using an injected client double."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import pytest
from anthropic import Omit
from nervos_core.application.model_completion import (
    MODEL_RESPONSE_INVALID,
    ModelProviderError,
    ModelRequest,
    ModelUsage,
    StopOutcome,
)
from nervos_models.anthropic import (
    PROVIDER_ID,
    AnthropicModelCompletion,
)

REQUEST = ModelRequest("system text", "user text", "opaque/model", 1024, 1500)
SYNTHETIC_CREDENTIAL = "SYNTHETIC-CREDENTIAL-VALUE-DO-NOT-LEAK"
SYNTHETIC_PROMPT = "SYNTHETIC-PROMPT-DO-NOT-LOG"
SYNTHETIC_ANSWER = "SYNTHETIC-ANSWER-DO-NOT-LOG"
SYNTHETIC_THINKING = "SYNTHETIC-HIDDEN-REASONING-DO-NOT-LEAK"


class _Create:
    def __init__(self, response: Any = None, error: BaseException | None = None) -> None:
        self.kwargs: list[dict[str, Any]] = []
        self._response = response
        self._error = error
        self.started = asyncio.Event()
        self._release = asyncio.Event()
        self.block = False

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        self.started.set()
        if self.block:
            await self._release.wait()
        if self._error is not None:
            raise self._error
        return self._response

    def release(self) -> None:
        self._release.set()


class _Client:
    def __init__(self, create: _Create) -> None:
        self.messages = create


def _block(block_type: str, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(type=block_type, **fields)


def _response(
    *blocks: Any,
    model: str = "claude-opus-5",
    stop_reason: str | None = "end_turn",
    usage: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), model=model, stop_reason=stop_reason, usage=usage)


def _text(value: str) -> Any:
    return _block("text", text=value)


def _adapter(create: _Create) -> AnthropicModelCompletion:
    return AnthropicModelCompletion(_Client(create))


@pytest.mark.anyio
async def test_request_translation_is_exact_and_single() -> None:
    create = _Create(_response(_text("hi")))
    await _adapter(create).complete(ModelRequest("sys", "usr", "opaque/model", 2048, 1500))
    assert len(create.kwargs) == 1
    kwargs = create.kwargs[0]
    assert kwargs["model"] == "opaque/model"
    assert kwargs["max_tokens"] == 2048
    assert kwargs["system"] == "sys"
    assert kwargs["messages"] == [{"role": "user", "content": "usr"}]
    assert kwargs["timeout"] == 1.5


@pytest.mark.anyio
async def test_multiline_request_text_is_preserved() -> None:
    prompt = "line one\n\tline two\r\n"
    create = _Create(_response(_text("hi")))
    await _adapter(create).complete(ModelRequest("sys", prompt, "m", 16, 1000))
    assert create.kwargs[0]["messages"] == [{"role": "user", "content": prompt}]


@pytest.mark.anyio
async def test_normal_response_is_normalized() -> None:
    create = _Create(
        _response(_text("hello"), usage=SimpleNamespace(input_tokens=9, output_tokens=4))
    )
    response = await _adapter(create).complete(REQUEST)
    assert response.text == "hello"
    assert response.model_provider == PROVIDER_ID
    assert response.model_name == "claude-opus-5"
    assert response.finish_reason is StopOutcome.STOP
    assert response.usage == ModelUsage(9, 4, None)
    assert response.usage is not None and response.usage.total_tokens is None


@pytest.mark.anyio
async def test_usage_cache_counters_do_not_produce_a_total() -> None:
    create = _Create(
        _response(
            _text("hello"),
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                cache_creation_input_tokens=900,
                cache_read_input_tokens=5000,
            ),
        )
    )
    response = await _adapter(create).complete(REQUEST)
    assert response.usage == ModelUsage(100, 20, None)


@pytest.mark.anyio
async def test_multiple_text_blocks_are_concatenated_in_order_without_separators() -> None:
    create = _Create(_response(_text("alpha"), _text("beta"), _text("gamma")))
    response = await _adapter(create).complete(REQUEST)
    assert response.text == "alphabetagamma"


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", StopOutcome.STOP),
        ("max_tokens", StopOutcome.INCOMPLETE),
        ("model_context_window_exceeded", StopOutcome.INCOMPLETE),
        ("refusal", StopOutcome.REFUSED),
    ],
)
@pytest.mark.anyio
async def test_stop_reason_mapping(stop_reason: str, expected: StopOutcome) -> None:
    create = _Create(_response(_text("hi"), stop_reason=stop_reason))
    response = await _adapter(create).complete(REQUEST)
    assert response.finish_reason is expected


@pytest.mark.parametrize(
    "stop_reason",
    ["stop_sequence", "tool_use", "pause_turn", "unknown_reason", None, ""],
)
@pytest.mark.anyio
async def test_unexpected_stop_reasons_fail_closed(stop_reason: str | None) -> None:
    create = _Create(_response(_text("hi"), stop_reason=stop_reason))
    with pytest.raises(ModelProviderError) as error:
        await _adapter(create).complete(REQUEST)
    assert error.value.code == MODEL_RESPONSE_INVALID


@pytest.mark.anyio
async def test_raw_stop_reason_is_not_a_successful_natural_stop() -> None:
    """Only `end_turn` may normalize to the canonical successful finish reason."""
    create = _Create(_response(_text("hi"), stop_reason="stop"))
    with pytest.raises(ModelProviderError) as error:
        await _adapter(create).complete(REQUEST)
    assert error.value.code == MODEL_RESPONSE_INVALID
    assert len(create.kwargs) == 1


@pytest.mark.anyio
async def test_hidden_reasoning_is_never_exposed_or_leaked() -> None:
    create = _Create(
        _response(
            _block("thinking", thinking=SYNTHETIC_THINKING, signature="sig"),
            _text("visible answer"),
            _block("redacted_thinking", data=SYNTHETIC_THINKING),
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
    )
    response = await _adapter(create).complete(REQUEST)
    assert response.text == "visible answer"
    assert SYNTHETIC_THINKING not in repr(response)
    assert SYNTHETIC_THINKING not in repr(response.usage)


@pytest.mark.parametrize(
    "block_type", ["tool_use", "server_tool_use", "mcp_tool_use", "future_block"]
)
@pytest.mark.anyio
async def test_unexpected_blocks_fail_closed(block_type: str) -> None:
    create = _Create(_response(_text("hi"), _block(block_type, id="x")))
    with pytest.raises(ModelProviderError) as error:
        await _adapter(create).complete(REQUEST)
    assert error.value.code == MODEL_RESPONSE_INVALID


@pytest.mark.parametrize(
    "content",
    [None, "just text", 42, [], [SimpleNamespace(type="text", text=7)], [SimpleNamespace()]],
)
@pytest.mark.anyio
async def test_malformed_content_fails_closed(content: Any) -> None:
    message = SimpleNamespace(content=content, model="m", stop_reason="end_turn", usage=None)
    create = _Create(message)
    with pytest.raises(ModelProviderError) as error:
        await _adapter(create).complete(REQUEST)
    assert error.value.code == MODEL_RESPONSE_INVALID


@pytest.mark.parametrize("model", [None, "", "   ", 42])
@pytest.mark.anyio
async def test_missing_model_identity_fails_closed(model: Any) -> None:
    create = _Create(_response(_text("hi"), model=model))
    with pytest.raises(ModelProviderError) as error:
        await _adapter(create).complete(REQUEST)
    assert error.value.code == MODEL_RESPONSE_INVALID


@pytest.mark.anyio
async def test_absent_usage_stays_absent() -> None:
    create = _Create(_response(_text("hi"), usage=None))
    response = await _adapter(create).complete(REQUEST)
    assert response.usage is None


@pytest.mark.parametrize(
    "usage",
    [
        SimpleNamespace(input_tokens=None, output_tokens=3),
        SimpleNamespace(input_tokens=-5, output_tokens=3),
        SimpleNamespace(input_tokens=True, output_tokens="many"),
    ],
)
@pytest.mark.anyio
async def test_untrustworthy_usage_values_become_null(usage: Any) -> None:
    create = _Create(_response(_text("hi"), usage=usage))
    response = await _adapter(create).complete(REQUEST)
    assert response.usage is not None
    assert response.usage.total_tokens is None
    if getattr(usage, "output_tokens", None) == 3:
        assert response.usage.output_tokens == 3


@pytest.mark.anyio
async def test_blank_text_block_is_returned_for_handler_validation() -> None:
    create = _Create(_response(_text("   ")))
    response = await _adapter(create).complete(REQUEST)
    assert response.text == "   "


def _error(exception_type: type[BaseException], status_code: int | None = None) -> BaseException:
    error = exception_type.__new__(exception_type)
    if status_code is not None:
        object.__setattr__(error, "status_code", status_code)
    return error


@pytest.mark.parametrize(
    ("exception_type", "status_code", "expected_code"),
    [
        (anthropic.AuthenticationError, 401, "model_authentication_failed"),
        (anthropic.PermissionDeniedError, 403, "model_permission_denied"),
        (anthropic.APITimeoutError, None, "model_timed_out"),
        (anthropic.DeadlineExceededError, 504, "model_timed_out"),
        (anthropic.RateLimitError, 429, "model_rate_limited"),
        (anthropic.APIConnectionError, None, "model_unavailable"),
        (anthropic.ServiceUnavailableError, 503, "model_unavailable"),
        (anthropic.OverloadedError, 529, "model_unavailable"),
        (anthropic.InternalServerError, 500, "model_unavailable"),
        (anthropic.BadRequestError, 400, "model_request_rejected"),
        (anthropic.RequestTooLargeError, 413, "model_request_rejected"),
        (anthropic.UnprocessableEntityError, 422, "model_request_rejected"),
        (anthropic.ConflictError, 409, "model_request_rejected"),
        (anthropic.NotFoundError, 404, "model_request_rejected"),
        (anthropic.APIResponseValidationError, None, MODEL_RESPONSE_INVALID),
        (anthropic.CredentialsError, None, "model_authentication_failed"),
        (anthropic.RetryableError, None, "model_unavailable"),
    ],
)
@pytest.mark.anyio
async def test_typed_exception_mapping(
    exception_type: type[BaseException], status_code: int | None, expected_code: str
) -> None:
    create = _Create(error=_error(exception_type, status_code))
    with pytest.raises(ModelProviderError) as error:
        await _adapter(create).complete(REQUEST)
    assert error.value.code == expected_code
    assert len(create.kwargs) == 1


def secret_request() -> ModelRequest:
    return ModelRequest("sys", SYNTHETIC_PROMPT, "opaque/model", 16, 1000)


@pytest.mark.anyio
async def test_credential_and_provider_detail_never_enter_the_public_error() -> None:
    failure = _error(anthropic.AuthenticationError, 401)
    object.__setattr__(failure, "message", f"invalid key {SYNTHETIC_CREDENTIAL}")
    create = _Create(error=failure)
    with pytest.raises(ModelProviderError) as error:
        await _adapter(create).complete(secret_request())
    assert SYNTHETIC_CREDENTIAL not in str(error.value)
    assert SYNTHETIC_CREDENTIAL not in repr(error.value)
    assert SYNTHETIC_PROMPT not in str(error.value)
    assert error.value.code == "model_authentication_failed"


@pytest.mark.anyio
async def test_cancellation_propagates_and_never_becomes_a_provider_failure() -> None:
    create = _Create(_response(_text("hi")))
    create.block = True
    task = asyncio.create_task(_adapter(create).complete(REQUEST))
    await asyncio.wait_for(create.started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(create.kwargs) == 1


def _fake_sdk_client(inherited: dict[str, Any]) -> tuple[type, list[dict[str, Any]]]:
    """A stand-in exposing the SDK's public header surface, plus names to report as inherited.

    The factory reads `platform_headers()` and the `default_headers` property, and may call
    `with_options`, exactly as it does against the real client.
    """
    copies: list[dict[str, Any]] = []

    class _FakeAsyncAnthropic:
        def __init__(self, **kwargs: Any) -> None:
            self.constructed: dict[str, Any] = kwargs

        @property
        def user_agent(self) -> str:
            return "AsyncAnthropic/Python 1.5.0"

        def platform_headers(self) -> dict[str, str]:
            return {"X-Stainless-Lang": "python"}

        @property
        def default_headers(self) -> dict[str, Any]:
            return {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "AsyncAnthropic/Python 1.5.0",
                "X-Stainless-Async": "async:asyncio",
                "anthropic-version": "2023-06-01",
                **self.platform_headers(),
                **self.constructed["default_headers"],
                **inherited,
            }

        def with_options(self, **kwargs: Any) -> Any:
            copies.append(kwargs)
            return self

    return _FakeAsyncAnthropic, copies


def test_production_client_disables_sdk_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production client must issue one request with provider retries disabled.

    Every keyword is enumerated, so the pinned canonical endpoint and the NervOS-owned
    authentication header are part of the frozen client contract and any further constructor
    argument would fail this test.
    """
    import nervos_models.anthropic as module

    fake, copies = _fake_sdk_client({})
    monkeypatch.setattr(module, "AsyncAnthropic", fake)
    client = module.create_anthropic_client("synthetic-key", timeout_seconds=1.5)
    assert isinstance(client, fake)
    captured = cast(Any, client).constructed
    assert set(captured) == {"api_key", "base_url", "max_retries", "timeout", "default_headers"}
    assert captured["api_key"] == "synthetic-key"
    assert captured["base_url"] == "https://api.anthropic.com"
    assert captured["max_retries"] == 0
    assert captured["timeout"] == 1.5
    headers = captured["default_headers"]
    assert headers["x-api-key"] == "synthetic-key"
    assert isinstance(headers["Authorization"], Omit)
    # Canonical headers are always frozen explicitly, including when no arbitrary ambient
    # name was inherited, so a same-name ambient collision cannot survive.
    assert len(copies) == 1
    final_headers = copies[0]["set_default_headers"]
    assert final_headers["Accept"] == "application/json"
    assert final_headers["Content-Type"] == "application/json"
    assert final_headers["User-Agent"] == "AsyncAnthropic/Python 1.5.0"
    assert final_headers["X-Stainless-Async"] == "async:asyncio"
    assert final_headers["Anthropic-Version"] == "2023-06-01"


def test_an_inherited_custom_header_name_is_removed_with_the_sdk_omit_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only names the factory did not authorize are removed, and by the SDK's own sentinel."""
    import nervos_models.anthropic as module

    fake, copies = _fake_sdk_client(
        {"X-Ambient-Extra": "synthetic-extra", "Anthropic-Beta": "synthetic-beta"}
    )
    monkeypatch.setattr(module, "AsyncAnthropic", fake)
    module.create_anthropic_client("synthetic-key", timeout_seconds=1.5)

    assert len(copies) == 1
    headers = copies[0]["set_default_headers"]
    assert isinstance(headers["X-Ambient-Extra"], Omit)
    assert isinstance(headers["Anthropic-Beta"], Omit)
    assert headers["x-api-key"] == "synthetic-key"
    assert isinstance(headers["Authorization"], Omit)
    # The SDK's own headers are restored with canonical values rather than omitted.
    assert headers["Anthropic-Version"] == "2023-06-01"
    assert headers["Accept"] == "application/json"
