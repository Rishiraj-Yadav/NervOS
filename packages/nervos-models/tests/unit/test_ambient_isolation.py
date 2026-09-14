"""Ambient provider SDK configuration cannot control a canonical NervOS request.

Both official SDKs read provider variables from the process environment while a client is
constructed, and the values they find outrank the credentials the SDK is given explicitly.
These tests export hostile values for every such variable, build the client through the real
production factory, and capture the prepared outgoing request with an in-process transport, so
no external request is ever made.
"""

from __future__ import annotations

import inspect
import os
from types import ModuleType
from typing import Any

import httpx2
import pytest
from nervos_core.application.model_completion import ModelProviderError, ModelRequest
from nervos_models import anthropic as anthropic_adapter
from nervos_models import openai as openai_adapter
from nervos_models.anthropic import AnthropicModelCompletion, create_anthropic_client
from nervos_models.openai import OpenAIModelCompletion, create_openai_client

NERVOS_ANTHROPIC_KEY = "SYNTHETIC-NERVOS-ANTHROPIC-KEY"
NERVOS_OPENAI_KEY = "SYNTHETIC-NERVOS-OPENAI-KEY"

AMBIENT_OPENAI_ENV_KEY = "AMBIENT-OPENAI-ENV-KEY"
AMBIENT_OPENAI_ADMIN_KEY = "AMBIENT-OPENAI-ADMIN-KEY"
AMBIENT_OPENAI_AUTHORIZATION = "AMBIENT-OPENAI-AUTHORIZATION"
AMBIENT_ANTHROPIC_ENV_KEY = "AMBIENT-ANTHROPIC-ENV-KEY"
AMBIENT_ANTHROPIC_AUTH_TOKEN = "AMBIENT-ANTHROPIC-AUTH-TOKEN"
AMBIENT_ANTHROPIC_API_KEY_HEADER = "AMBIENT-ANTHROPIC-X-API-KEY"
AMBIENT_ANTHROPIC_AUTHORIZATION = "AMBIENT-ANTHROPIC-AUTHORIZATION"
AMBIENT_ORGANIZATION = "AMBIENT-ORGANIZATION"
AMBIENT_PROJECT = "AMBIENT-PROJECT"
AMBIENT_REDIRECT_BASE_URL = "https://ambient-redirect.invalid"
# Arbitrary, non-authentication names an operator could put in the ambient custom-headers
# variable. None of them is a header the SDK or NervOS generates for a canonical request, and
# none of them is authorized by the Stage B configuration contract.
AMBIENT_EXTRA_HEADER = "X-Ambient-Extra"
AMBIENT_OPENAI_BETA_HEADER = "OpenAI-Beta"
AMBIENT_ANTHROPIC_BETA_HEADER = "Anthropic-Beta"
AMBIENT_OPENAI_EXTRA = "AMBIENT-OPENAI-EXTRA"
AMBIENT_OPENAI_BETA = "AMBIENT-OPENAI-BETA"
AMBIENT_OPENAI_USER_AGENT = "AMBIENT-OPENAI-USER-AGENT"
AMBIENT_OPENAI_ASYNC = "AMBIENT-OPENAI-ASYNC"
AMBIENT_ANTHROPIC_EXTRA = "AMBIENT-ANTHROPIC-EXTRA"
AMBIENT_ANTHROPIC_BETA = "AMBIENT-ANTHROPIC-BETA"
AMBIENT_ANTHROPIC_USER_AGENT = "AMBIENT-ANTHROPIC-USER-AGENT"
AMBIENT_ANTHROPIC_VERSION = "AMBIENT-ANTHROPIC-VERSION"
AMBIENT_CONTENT_TYPE = "text/plain"

PROVIDER_AUTH_SENTINELS = (
    AMBIENT_OPENAI_ENV_KEY,
    AMBIENT_OPENAI_ADMIN_KEY,
    AMBIENT_OPENAI_AUTHORIZATION,
    AMBIENT_ANTHROPIC_ENV_KEY,
    AMBIENT_ANTHROPIC_AUTH_TOKEN,
    AMBIENT_ANTHROPIC_API_KEY_HEADER,
    AMBIENT_ANTHROPIC_AUTHORIZATION,
)
SCOPING_SENTINELS = (AMBIENT_ORGANIZATION, AMBIENT_PROJECT)
CUSTOM_HEADER_SENTINELS = (
    AMBIENT_OPENAI_EXTRA,
    AMBIENT_OPENAI_BETA,
    AMBIENT_OPENAI_USER_AGENT,
    AMBIENT_OPENAI_ASYNC,
    AMBIENT_ANTHROPIC_EXTRA,
    AMBIENT_ANTHROPIC_BETA,
    AMBIENT_ANTHROPIC_USER_AGENT,
    AMBIENT_ANTHROPIC_VERSION,
    AMBIENT_CONTENT_TYPE,
)
# Every hostile value an operator's shell could contribute, across all ambient channels.
ALL_AMBIENT_SENTINELS = PROVIDER_AUTH_SENTINELS + SCOPING_SENTINELS + CUSTOM_HEADER_SENTINELS

OPENAI_RESPONSE_BODY: dict[str, Any] = {
    "id": "resp_captured",
    "object": "response",
    "created_at": 0,
    "model": "opaque/model",
    "status": "completed",
    "error": None,
    "incomplete_details": None,
    "parallel_tool_calls": False,
    "tool_choice": "auto",
    "tools": [],
    "output": [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "captured"}],
        }
    ],
    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
}
ANTHROPIC_RESPONSE_BODY: dict[str, Any] = {
    "id": "msg_captured",
    "type": "message",
    "role": "assistant",
    "model": "opaque/model",
    "content": [{"type": "text", "text": "captured"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
REQUEST = ModelRequest("system", "user", "opaque/model", 64, 4000)


class _Capture:
    """Collect the prepared outgoing requests a client would put on the wire."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.requests: list[httpx2.Request] = []
        self._body = body

    def transport(self) -> httpx2.MockTransport:
        def handler(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            return httpx2.Response(200, json=self._body)

        return httpx2.MockTransport(handler)

    @property
    def request(self) -> httpx2.Request:
        assert len(self.requests) == 1, self.requests
        return self.requests[0]

    def header(self, name: str) -> str | None:
        return self.request.headers.get(name)

    def sentinels(self, values: tuple[str, ...]) -> list[str]:
        """Return the watched values that appear anywhere in the prepared request."""
        body = self.request.content.decode(errors="replace")
        serialized = f"{self.request.url} {dict(self.request.headers)} {body}"
        return [value for value in values if value in serialized]


def _bind_transport(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, class_name: str, transport: Any
) -> None:
    """Route the factory's client through an in-process transport instead of the network.

    The seam is the SDK's own public ``http_client`` argument, so the client under test is the
    real SDK client built by the real production factory.
    """
    real_client = getattr(module, class_name)

    def _transport_bound_client(**kwargs: Any) -> Any:
        return real_client(**{**kwargs, "http_client": httpx2.AsyncClient(transport=transport)})

    monkeypatch.setattr(module, class_name, _transport_bound_client)


@pytest.fixture
def hostile_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Export every ambient provider variable the locked SDKs read, with hostile values."""
    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_ADMIN_KEY",
        "NERVOS_ANTHROPIC_API_KEY",
        "NERVOS_OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT_OPENAI_ENV_KEY)
    monkeypatch.setenv("OPENAI_ADMIN_KEY", AMBIENT_OPENAI_ADMIN_KEY)
    monkeypatch.setenv("OPENAI_BASE_URL", AMBIENT_REDIRECT_BASE_URL)
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS",
        f"Content-Type: {AMBIENT_CONTENT_TYPE}\n"
        f"Accept: {AMBIENT_CONTENT_TYPE}\n"
        f"User-Agent: {AMBIENT_OPENAI_USER_AGENT}\n"
        f"X-Stainless-Async: {AMBIENT_OPENAI_ASYNC}\n"
        f"Authorization: Bearer {AMBIENT_OPENAI_AUTHORIZATION}\n"
        f"{AMBIENT_EXTRA_HEADER}: {AMBIENT_OPENAI_EXTRA}\n"
        f"{AMBIENT_OPENAI_BETA_HEADER}: {AMBIENT_OPENAI_BETA}",
    )
    monkeypatch.setenv("OPENAI_ORG_ID", AMBIENT_ORGANIZATION)
    monkeypatch.setenv("OPENAI_PROJECT_ID", AMBIENT_PROJECT)

    monkeypatch.setenv("ANTHROPIC_API_KEY", AMBIENT_ANTHROPIC_ENV_KEY)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", AMBIENT_ANTHROPIC_AUTH_TOKEN)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", AMBIENT_REDIRECT_BASE_URL)
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS",
        f"Content-Type: {AMBIENT_CONTENT_TYPE}\n"
        f"Accept: {AMBIENT_CONTENT_TYPE}\n"
        f"User-Agent: {AMBIENT_ANTHROPIC_USER_AGENT}\n"
        f"Anthropic-Version: {AMBIENT_ANTHROPIC_VERSION}\n"
        f"x-api-key: {AMBIENT_ANTHROPIC_API_KEY_HEADER}\n"
        f"Authorization: Bearer {AMBIENT_ANTHROPIC_AUTHORIZATION}\n"
        f"{AMBIENT_EXTRA_HEADER}: {AMBIENT_ANTHROPIC_EXTRA}\n"
        f"{AMBIENT_ANTHROPIC_BETA_HEADER}: {AMBIENT_ANTHROPIC_BETA}",
    )


@pytest.mark.anyio
async def test_openai_authentication_comes_only_from_the_nervos_credential(
    monkeypatch: pytest.MonkeyPatch, hostile_environment: None
) -> None:
    capture = _Capture(OPENAI_RESPONSE_BODY)
    _bind_transport(monkeypatch, openai_adapter, "AsyncOpenAI", capture.transport())

    client = create_openai_client(NERVOS_OPENAI_KEY, 5.0)
    result = await OpenAIModelCompletion(client).complete(REQUEST)

    assert str(capture.request.url) == "https://api.openai.com/v1/responses"
    assert capture.header("authorization") == f"Bearer {NERVOS_OPENAI_KEY}"
    assert capture.header("openai-organization") is None
    assert capture.header("openai-project") is None
    assert capture.sentinels(ALL_AMBIENT_SENTINELS) == []
    assert result.text == "captured"


@pytest.mark.anyio
async def test_anthropic_authentication_comes_only_from_the_nervos_credential(
    monkeypatch: pytest.MonkeyPatch, hostile_environment: None
) -> None:
    capture = _Capture(ANTHROPIC_RESPONSE_BODY)
    _bind_transport(monkeypatch, anthropic_adapter, "AsyncAnthropic", capture.transport())

    client = create_anthropic_client(NERVOS_ANTHROPIC_KEY, 5.0)
    result = await AnthropicModelCompletion(client).complete(REQUEST)

    assert str(capture.request.url) == "https://api.anthropic.com/v1/messages"
    assert capture.header("x-api-key") == NERVOS_ANTHROPIC_KEY
    # No bearer credential is presented at all, so an ambient token has no second channel.
    assert capture.header("authorization") is None
    assert capture.sentinels(ALL_AMBIENT_SENTINELS) == []
    assert result.text == "captured"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("module", "class_name", "factory_name", "adapter_name", "key", "body", "origin"),
    [
        (
            openai_adapter,
            "AsyncOpenAI",
            "create_openai_client",
            "OpenAIModelCompletion",
            NERVOS_OPENAI_KEY,
            OPENAI_RESPONSE_BODY,
            "https://api.openai.com/v1/responses",
        ),
        (
            anthropic_adapter,
            "AsyncAnthropic",
            "create_anthropic_client",
            "AnthropicModelCompletion",
            NERVOS_ANTHROPIC_KEY,
            ANTHROPIC_RESPONSE_BODY,
            "https://api.anthropic.com/v1/messages",
        ),
    ],
)
async def test_ambient_base_url_variables_cannot_redirect_a_canonical_provider(
    monkeypatch: pytest.MonkeyPatch,
    hostile_environment: None,
    module: ModuleType,
    class_name: str,
    factory_name: str,
    adapter_name: str,
    key: str,
    body: dict[str, Any],
    origin: str,
) -> None:
    """The ambient base-URL variables are only one hostile input; the destination stays pinned."""
    capture = _Capture(body)
    _bind_transport(monkeypatch, module, class_name, capture.transport())

    client = getattr(module, factory_name)(key, 5.0)
    await getattr(module, adapter_name)(client).complete(REQUEST)

    assert str(capture.request.url) == origin
    assert AMBIENT_REDIRECT_BASE_URL not in str(capture.request.url)


@pytest.mark.anyio
async def test_no_ambient_credential_sentinel_reaches_a_provider_failure(
    monkeypatch: pytest.MonkeyPatch, hostile_environment: None
) -> None:
    """Hostile ambient values must not surface through a normalized provider failure."""
    body = {**OPENAI_RESPONSE_BODY, "status": "failed"}
    capture = _Capture(body)
    _bind_transport(monkeypatch, openai_adapter, "AsyncOpenAI", capture.transport())

    client = create_openai_client(NERVOS_OPENAI_KEY, 5.0)
    with pytest.raises(ModelProviderError) as caught:
        await OpenAIModelCompletion(client).complete(REQUEST)

    assert caught.value.code == "model_unavailable"
    chain = captured_exception(caught.value)
    rendered = f"{caught.value} {caught.value.code} {caught.value.usage!r} {chain}"
    for sentinel in ALL_AMBIENT_SENTINELS:
        assert sentinel not in rendered


def captured_exception(error: BaseException) -> str:
    """Render a whole exception chain, so a leak anywhere in it would be visible."""
    parts: list[str] = []
    current: BaseException | None = error
    while current is not None:
        parts.append(f"{current!r} {current!s} {vars(current)!r}")
        current = current.__cause__
    return " ".join(parts)


@pytest.mark.anyio
async def test_client_construction_and_execution_never_mutate_the_process_environment(
    monkeypatch: pytest.MonkeyPatch, hostile_environment: None
) -> None:
    """Isolation must come from the SDK's public API, never from process-global mutation."""
    capture = _Capture(OPENAI_RESPONSE_BODY)
    _bind_transport(monkeypatch, openai_adapter, "AsyncOpenAI", capture.transport())
    before = dict(os.environ)

    client = create_openai_client(NERVOS_OPENAI_KEY, 5.0)
    await OpenAIModelCompletion(client).complete(REQUEST)

    assert dict(os.environ) == before


def test_adapter_modules_never_read_or_mutate_the_process_environment() -> None:
    """Neither adapter module may consult or change the environment at all."""
    for module in (openai_adapter, anthropic_adapter):
        source = inspect.getsource(module)
        assert "os.environ" not in source, module.__name__
        assert "putenv" not in source, module.__name__
        assert "setenv" not in source, module.__name__


@pytest.mark.anyio
@pytest.mark.parametrize(
    (
        "module",
        "class_name",
        "factory_name",
        "adapter_name",
        "key",
        "body",
        "extra_header",
        "beta_header",
        "extra_sentinel",
        "beta_sentinel",
    ),
    [
        (
            openai_adapter,
            "AsyncOpenAI",
            "create_openai_client",
            "OpenAIModelCompletion",
            NERVOS_OPENAI_KEY,
            OPENAI_RESPONSE_BODY,
            AMBIENT_EXTRA_HEADER,
            AMBIENT_OPENAI_BETA_HEADER,
            AMBIENT_OPENAI_EXTRA,
            AMBIENT_OPENAI_BETA,
        ),
        (
            anthropic_adapter,
            "AsyncAnthropic",
            "create_anthropic_client",
            "AnthropicModelCompletion",
            NERVOS_ANTHROPIC_KEY,
            ANTHROPIC_RESPONSE_BODY,
            AMBIENT_EXTRA_HEADER,
            AMBIENT_ANTHROPIC_BETA_HEADER,
            AMBIENT_ANTHROPIC_EXTRA,
            AMBIENT_ANTHROPIC_BETA,
        ),
    ],
)
async def test_arbitrary_ambient_custom_headers_never_reach_a_canonical_provider(
    monkeypatch: pytest.MonkeyPatch,
    hostile_environment: None,
    module: ModuleType,
    class_name: str,
    factory_name: str,
    adapter_name: str,
    key: str,
    body: dict[str, Any],
    extra_header: str,
    beta_header: str,
    extra_sentinel: str,
    beta_sentinel: str,
) -> None:
    """Stage B supports one provider-environment input per provider: its API key.

    An operator's ambient custom-headers variable is therefore not a configuration channel,
    and a name in it - anticipated or not - must not reach a canonical request. The factory
    neutralizes exactly this by asking the SDK which names it will send and removing every
    name NervOS did not authorize, so this asserts the absence of the forwarded name rather
    than the presence of a documented exception.
    """
    capture = _Capture(body)
    _bind_transport(monkeypatch, module, class_name, capture.transport())

    client = getattr(module, factory_name)(key, 5.0)
    await getattr(module, adapter_name)(client).complete(REQUEST)

    assert capture.header(extra_header) is None
    assert capture.header(beta_header) is None
    assert capture.sentinels(CUSTOM_HEADER_SENTINELS) == []
    # The canonical request is otherwise intact: the ambient layer is removed, not replaced.
    assert capture.header("content-type") == "application/json"
    assert capture.header("accept") == "application/json"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("module", "class_name", "factory_name", "adapter_name", "key", "body", "expected"),
    [
        (
            openai_adapter,
            "AsyncOpenAI",
            "create_openai_client",
            "OpenAIModelCompletion",
            NERVOS_OPENAI_KEY,
            OPENAI_RESPONSE_BODY,
            (
                ("content-type", "application/json"),
                ("accept", "application/json"),
                ("x-stainless-lang", "python"),
                ("x-stainless-async", "async:asyncio"),
            ),
        ),
        (
            anthropic_adapter,
            "AsyncAnthropic",
            "create_anthropic_client",
            "AnthropicModelCompletion",
            NERVOS_ANTHROPIC_KEY,
            ANTHROPIC_RESPONSE_BODY,
            (
                ("content-type", "application/json"),
                ("accept", "application/json"),
                ("anthropic-version", "2023-06-01"),
                ("x-stainless-lang", "python"),
            ),
        ),
    ],
)
async def test_neutralizing_ambient_headers_leaves_the_sdk_canonical_headers_intact(
    monkeypatch: pytest.MonkeyPatch,
    hostile_environment: None,
    module: ModuleType,
    class_name: str,
    factory_name: str,
    adapter_name: str,
    key: str,
    body: dict[str, Any],
    expected: tuple[tuple[str, str], ...],
) -> None:
    """Removal is by provenance, so a header the SDK owns is never removed with the ambient ones.

    This guards the boundary in the other direction from the absence test: a canonical request
    still carries every header the SDK generates for it, including the API-version header the
    Anthropic SDK pins. A name the SDK stops owning would show up here rather than only as a
    rejected request.
    """
    capture = _Capture(body)
    _bind_transport(monkeypatch, module, class_name, capture.transport())

    client = getattr(module, factory_name)(key, 5.0)
    await getattr(module, adapter_name)(client).complete(REQUEST)

    for name, value in expected:
        assert capture.header(name) == value, name
    assert capture.header("user-agent") not in {
        AMBIENT_OPENAI_USER_AGENT,
        AMBIENT_ANTHROPIC_USER_AGENT,
    }
    assert capture.sentinels(ALL_AMBIENT_SENTINELS) == []
