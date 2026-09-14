"""Deterministic contract tests for the OpenAI Responses adapter."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import openai
import pytest
from nervos_core.application.model_completion import ModelProviderError, ModelRequest, StopOutcome
from nervos_models.openai import OpenAIModelCompletion
from openai import Omit

REQUEST = ModelRequest("system", "user", "opaque-model", 99, 1500)


class Create:
    def __init__(self, response: Any = None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def adapter(create: Create) -> OpenAIModelCompletion:
    return OpenAIModelCompletion(SimpleNamespace(responses=SimpleNamespace(create=create)))


def response(*items: Any, status: str = "completed", reason: str | None = None, **kw: Any) -> Any:
    return SimpleNamespace(
        status=status,
        output=list(items),
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        error=kw.get("error"),
        usage=kw.get("usage", SimpleNamespace(input_tokens=2, output_tokens=3, total_tokens=7)),
        model=kw.get("model", "provider-alias"),
    )


def message(*parts: Any) -> Any:
    return SimpleNamespace(type="message", role="assistant", content=list(parts))


def text(value: str) -> Any:
    return SimpleNamespace(type="output_text", text=value)


@pytest.mark.anyio
async def test_exact_stateless_non_streaming_request_and_alias_is_accepted() -> None:
    create = Create(response(message(text("a"), text("b"))))
    result = await adapter(create).complete(REQUEST)
    assert create.calls == [
        {
            "model": "opaque-model",
            "instructions": "system",
            "input": "user",
            "max_output_tokens": 99,
            "store": False,
            "background": False,
            "stream": False,
            "timeout": 1.5,
        }
    ]
    assert result.text == "ab"
    assert result.model_name == "opaque-model"
    assert result.finish_reason is StopOutcome.STOP
    assert result.usage is not None and result.usage.values() == (2, 3, 7)


@pytest.mark.anyio
async def test_reasoning_is_ignored() -> None:
    reasoning = SimpleNamespace(type="reasoning", summary="SECRET-REASONING")
    result = await adapter(Create(response(reasoning, message(text("visible"))))).complete(REQUEST)
    assert result.text == "visible" and "SECRET" not in result.text


@pytest.mark.anyio
async def test_refusal_dominates_text_without_leaking() -> None:
    refusal = SimpleNamespace(type="refusal", refusal="SECRET-REFUSAL")
    with pytest.raises(ModelProviderError) as caught:
        await adapter(Create(response(message(text("partial"), refusal)))).complete(REQUEST)
    assert caught.value.code == "model_refused"
    assert "SECRET" not in str(caught.value) and "partial" not in str(caught.value)


@pytest.mark.parametrize(
    "item_type", ["function_call", "custom_tool_call", "mcp_call", "computer_call", "unknown"]
)
@pytest.mark.anyio
async def test_tool_and_unknown_outputs_fail_closed(item_type: str) -> None:
    with pytest.raises(ModelProviderError) as caught:
        await adapter(Create(response(SimpleNamespace(type=item_type)))).complete(REQUEST)
    assert caught.value.code == "model_response_invalid"


@pytest.mark.parametrize(
    ("reason", "outcome"),
    [("max_output_tokens", StopOutcome.INCOMPLETE), ("content_filter", StopOutcome.REFUSED)],
)
@pytest.mark.anyio
async def test_canonical_incomplete_outcomes(reason: str, outcome: StopOutcome) -> None:
    result = await adapter(
        Create(response(message(text("partial")), status="incomplete", reason=reason))
    ).complete(REQUEST)
    assert result.finish_reason is outcome and result.text == ""


@pytest.mark.parametrize(
    ("reason", "outcome"),
    [("max_output_tokens", StopOutcome.INCOMPLETE), ("content_filter", StopOutcome.REFUSED)],
)
@pytest.mark.anyio
async def test_non_success_outcome_is_canonical_without_any_output_item(
    reason: str, outcome: StopOutcome
) -> None:
    """A truncated or filtered response may return no output item and still be canonical."""
    create = Create(response(status="incomplete", reason=reason))

    result = await adapter(create).complete(REQUEST)

    assert result.finish_reason is outcome
    assert result.text == ""
    assert result.model_provider == "openai"
    assert len(create.calls) == 1


@pytest.mark.anyio
async def test_completed_response_without_any_output_item_is_invalid() -> None:
    """The relaxed text rule applies only to a non-success outcome, never to a completion."""
    with pytest.raises(ModelProviderError) as caught:
        await adapter(Create(response())).complete(REQUEST)

    assert caught.value.code == "model_response_invalid"


@pytest.mark.anyio
async def test_hidden_reasoning_never_surfaces_for_a_non_success_outcome() -> None:
    reasoning = SimpleNamespace(type="reasoning", summary="SECRET-REASONING")

    result = await adapter(
        Create(response(reasoning, status="incomplete", reason="max_output_tokens"))
    ).complete(REQUEST)

    assert result.finish_reason is StopOutcome.INCOMPLETE
    assert result.text == "" and "SECRET" not in result.text


@pytest.mark.parametrize("item_type", ["function_call", "mcp_call", "unknown"])
@pytest.mark.anyio
async def test_a_tool_item_still_fails_closed_for_a_non_success_status(item_type: str) -> None:
    """Item validity is enforced for every outcome, so a tool item never rides along."""
    with pytest.raises(ModelProviderError) as caught:
        await adapter(
            Create(
                response(
                    SimpleNamespace(type=item_type),
                    status="incomplete",
                    reason="max_output_tokens",
                )
            )
        ).complete(REQUEST)

    assert caught.value.code == "model_response_invalid"


@pytest.mark.anyio
async def test_explicit_refusal_dominates_a_non_success_status_without_leaking() -> None:
    refusal = SimpleNamespace(type="refusal", refusal="SECRET-REFUSAL")

    with pytest.raises(ModelProviderError) as caught:
        await adapter(
            Create(
                response(
                    message(text("SECRET-PARTIAL"), refusal),
                    status="incomplete",
                    reason="max_output_tokens",
                )
            )
        ).complete(REQUEST)

    assert caught.value.code == "model_refused"
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "reason", "code"),
    [
        ("incomplete", "max_messages", "model_response_invalid"),
        ("incomplete", "steered", "model_response_invalid"),
        ("incomplete", None, "model_response_invalid"),
        ("failed", None, "model_unavailable"),
        ("queued", None, "model_response_invalid"),
        ("in_progress", None, "model_response_invalid"),
        ("cancelled", None, "model_response_invalid"),
        ("future", None, "model_response_invalid"),
    ],
)
@pytest.mark.anyio
async def test_status_mapping(status: str, reason: str | None, code: str) -> None:
    with pytest.raises(ModelProviderError) as caught:
        await adapter(
            Create(response(message(text("partial")), status=status, reason=reason))
        ).complete(REQUEST)
    assert caught.value.code == code


@pytest.mark.anyio
async def test_structured_response_error_is_safe() -> None:
    raw = SimpleNamespace(code="server_error", message="SECRET-RAW-ERROR")
    with pytest.raises(ModelProviderError) as caught:
        await adapter(Create(response(message(text("x")), error=raw))).complete(REQUEST)
    assert caught.value.code == "model_unavailable"
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize("value", [-1, True, "2", None])
@pytest.mark.anyio
async def test_malformed_usage_fields_become_unknown(value: Any) -> None:
    usage = SimpleNamespace(input_tokens=value, output_tokens=3, total_tokens=value)
    result = await adapter(Create(response(message(text("ok")), usage=usage))).complete(REQUEST)
    assert result.usage is not None
    assert result.usage.input_tokens is None and result.usage.total_tokens is None


@pytest.mark.anyio
async def test_external_cancellation_propagates() -> None:
    with pytest.raises(asyncio.CancelledError):
        await adapter(Create(error=asyncio.CancelledError())).complete(REQUEST)


@pytest.mark.anyio
async def test_empty_visible_output_is_invalid() -> None:
    with pytest.raises(ModelProviderError) as caught:
        await adapter(Create(response(message(text(""))))).complete(REQUEST)
    assert caught.value.code == "model_response_invalid"


@pytest.mark.parametrize(
    "error_type,code",
    [
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
        (openai.APIResponseValidationError, "model_response_invalid"),
    ],
)
@pytest.mark.anyio
async def test_typed_error_mapping(error_type: type[BaseException], code: str) -> None:
    error = error_type.__new__(error_type)
    with pytest.raises(ModelProviderError) as caught:
        await adapter(Create(error=error)).complete(REQUEST)
    assert caught.value.code == code


def _fake_sdk_client(inherited: dict[str, Any]) -> tuple[type, list[dict[str, Any]]]:
    """A stand-in exposing the SDK's public header surface, plus names to report as inherited.

    The factory reads `platform_headers()` and the `default_headers` property, and may call
    `with_options`, exactly as it does against the real client.
    """
    copies: list[dict[str, Any]] = []

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            self.constructed: dict[str, Any] = kwargs

        @property
        def user_agent(self) -> str:
            return "AsyncOpenAI/Python 3.13.0"

        def platform_headers(self) -> dict[str, str]:
            return {"X-Stainless-Lang": "python"}

        @property
        def default_headers(self) -> dict[str, Any]:
            return {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "AsyncOpenAI/Python 3.13.0",
                "X-Stainless-Async": "async:asyncio",
                **self.platform_headers(),
                **self.constructed["default_headers"],
                **inherited,
            }

        def with_options(self, **kwargs: Any) -> Any:
            copies.append(kwargs)
            return self

    return _FakeAsyncOpenAI, copies


def test_production_client_pins_the_canonical_endpoint_with_retries_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The construction kwargs are the frozen client contract.

    Every keyword is enumerated, so the pinned endpoint, the disabled retries, and the
    NervOS-owned authentication and scoping headers are all part of that contract and any
    further constructor argument fails this test.
    """
    import nervos_models.openai as module

    fake, copies = _fake_sdk_client({})
    monkeypatch.setattr(module, "AsyncOpenAI", fake)
    client = module.create_openai_client("synthetic-key", 1.5)

    assert isinstance(client, fake)
    captured = cast(Any, client).constructed
    assert set(captured) == {"api_key", "base_url", "max_retries", "timeout", "default_headers"}
    assert captured["api_key"] == "synthetic-key"
    assert captured["base_url"] == "https://api.openai.com/v1"
    assert captured["max_retries"] == 0
    assert captured["timeout"] == 1.5
    headers = captured["default_headers"]
    assert headers["Authorization"] == "Bearer synthetic-key"
    assert isinstance(headers["OpenAI-Organization"], Omit)
    assert isinstance(headers["OpenAI-Project"], Omit)
    # Canonical headers are always frozen explicitly, including when no arbitrary ambient
    # name was inherited, so a same-name ambient collision cannot survive.
    assert len(copies) == 1
    final_headers = copies[0]["set_default_headers"]
    assert final_headers["Accept"] == "application/json"
    assert final_headers["Content-Type"] == "application/json"
    assert final_headers["User-Agent"] == "AsyncOpenAI/Python 3.13.0"
    assert final_headers["X-Stainless-Async"] == "async:asyncio"


def test_an_inherited_custom_header_name_is_removed_with_the_sdk_omit_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only names the factory did not authorize are removed, and by the SDK's own sentinel."""
    import nervos_models.openai as module

    fake, copies = _fake_sdk_client(
        {"X-Ambient-Extra": "synthetic-extra", "OpenAI-Beta": "synthetic-beta"}
    )
    monkeypatch.setattr(module, "AsyncOpenAI", fake)
    module.create_openai_client("synthetic-key", 1.5)

    assert len(copies) == 1
    headers = copies[0]["set_default_headers"]
    assert isinstance(headers["X-Ambient-Extra"], Omit)
    assert isinstance(headers["OpenAI-Beta"], Omit)
    assert headers["Authorization"] == "Bearer synthetic-key"
    assert isinstance(headers["OpenAI-Organization"], Omit)
    assert isinstance(headers["OpenAI-Project"], Omit)
    # The SDK's own headers are restored with canonical values rather than omitted.
    assert headers["X-Stainless-Lang"] == "python"
    assert headers["Accept"] == "application/json"
