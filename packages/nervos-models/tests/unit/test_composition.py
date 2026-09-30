"""Deterministic composition tests for the two production providers."""

from __future__ import annotations

from typing import Any, cast

import pytest
from nervos_models import composition
from nervos_models.anthropic import PROVIDER_ID as ANTHROPIC_ID
from nervos_models.anthropic import create_anthropic_client
from nervos_models.gemini import PROVIDER_ID as GEMINI_ID
from nervos_models.gemini import create_gemini_client
from nervos_models.openai import PROVIDER_ID as OPENAI_ID
from nervos_models.openai import create_openai_client

ANTHROPIC_KEY = "SYNTHETIC-ANTHROPIC-CREDENTIAL"
OPENAI_KEY = "SYNTHETIC-OPENAI-CREDENTIAL"
GEMINI_KEY = "SYNTHETIC-GEMINI-CREDENTIAL"


def test_both_providers_are_known_without_any_credential() -> None:
    """Absent credentials make a provider unavailable, never unknown."""
    composed = composition.compose_model_providers(None, None, None)

    assert composed.catalog.is_known(ANTHROPIC_ID)
    assert composed.catalog.is_known(OPENAI_ID)
    assert composed.catalog.is_known(GEMINI_ID)
    assert not composed.catalog.is_configured(ANTHROPIC_ID)
    assert not composed.catalog.is_configured(OPENAI_ID)
    assert not composed.catalog.is_configured(GEMINI_ID)
    assert composed.anthropic_client is None
    assert composed.openai_client is None
    assert composed.gemini_client is None


def test_only_the_anthropic_credential_configures_anthropic() -> None:
    composed = composition.compose_model_providers(ANTHROPIC_KEY, None)

    assert composed.catalog.is_configured(ANTHROPIC_ID)
    assert not composed.catalog.is_configured(OPENAI_ID)
    assert composed.anthropic_client is not None
    assert composed.openai_client is None


def test_only_the_openai_credential_configures_openai() -> None:
    composed = composition.compose_model_providers(None, OPENAI_KEY)

    assert composed.catalog.is_known(ANTHROPIC_ID)
    assert not composed.catalog.is_configured(ANTHROPIC_ID)
    assert composed.catalog.is_configured(OPENAI_ID)
    assert composed.anthropic_client is None
    assert composed.openai_client is not None


def test_both_credentials_configure_both_independent_providers() -> None:
    composed = composition.compose_model_providers(ANTHROPIC_KEY, OPENAI_KEY)

    assert composed.catalog.is_configured(ANTHROPIC_ID)
    assert composed.catalog.is_configured(OPENAI_ID)
    assert composed.catalog.resolve(ANTHROPIC_ID) is not composed.catalog.resolve(OPENAI_ID)


@pytest.mark.parametrize(
    "alias", ["Anthropic", "ANTHROPIC", "open-ai", "OpenAI", "gpt", "google", "vertex"]
)
def test_no_alias_is_accepted_for_either_provider(alias: str) -> None:
    composed = composition.compose_model_providers(ANTHROPIC_KEY, OPENAI_KEY)

    assert not composed.catalog.is_known(alias)


def test_an_unconfigured_provider_never_resolves_to_the_other() -> None:
    """Portability is explicit configuration; a configured provider is not a substitute."""
    from nervos_core.application.model_providers import ModelProviderUnavailable

    composed = composition.compose_model_providers(ANTHROPIC_KEY, None)

    assert composed.catalog.resolve(ANTHROPIC_ID).__class__.__name__ == "AnthropicModelCompletion"
    with pytest.raises(ModelProviderUnavailable):
        composed.catalog.resolve(OPENAI_ID)


def test_only_gemini_credential_configures_gemini() -> None:
    composed = composition.compose_model_providers(None, None, GEMINI_KEY)
    assert composed.catalog.is_known(GEMINI_ID)
    assert composed.catalog.is_configured(GEMINI_ID)
    assert not composed.catalog.is_configured(ANTHROPIC_ID)
    assert not composed.catalog.is_configured(OPENAI_ID)
    assert composed.gemini_client is not None
    assert composed.anthropic_client is None and composed.openai_client is None


@pytest.mark.anyio
async def test_shutdown_closes_only_the_clients_that_exist() -> None:
    composed = composition.compose_model_providers(OPENAI_KEY, None)
    closed: list[str] = []

    class _Client:
        async def close(self) -> None:
            closed.append("closed")

    composed = composition.ModelProviderComposition(composed.catalog, None, _Client())  # type: ignore[arg-type]
    await composition.close_model_providers(composed)

    assert closed == ["closed"]


@pytest.mark.anyio
async def test_shutdown_closes_both_configured_clients() -> None:
    closed: list[str] = []

    class _AsyncClient:
        def __init__(self, name: str) -> None:
            self._name = name

        async def close(self) -> None:
            closed.append(self._name)

    class _GeminiClient:
        def __init__(self, name: str, *, async_close: bool = False) -> None:
            self._name = name
            self.aio = self if async_close else None

        def close(self) -> None:
            closed.append(self._name)

        async def aclose(self) -> None:
            closed.append(f"{self._name}-aio")

    composed = composition.ModelProviderComposition(
        composition.compose_model_providers(None, None).catalog,
        _AsyncClient("anthropic"),  # type: ignore[arg-type]
        _AsyncClient("openai"),  # type: ignore[arg-type]
        _GeminiClient("gemini", async_close=True),  # type: ignore[arg-type]
    )
    await composition.close_model_providers(composed)

    assert closed == ["anthropic", "openai", "gemini-aio", "gemini"]


def test_gemini_client_pins_the_developer_api_and_single_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "AMBIENT-KEY-MUST-NOT-WIN")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://ambient.invalid")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_GENAI_USE_ENTERPRISE", "true")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "C:/ambient/credentials.invalid")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "ambient-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "ambient-location")
    client = create_gemini_client(GEMINI_KEY, 4.0)
    api_client = cast(Any, client)._api_client
    assert api_client.vertexai is False
    assert api_client._http_options.base_url == "https://generativelanguage.googleapis.com"
    assert api_client.api_key == GEMINI_KEY
    assert api_client._use_google_auth_sync() is False
    assert api_client._use_google_auth_async() is False
    assert api_client._http_options.retry_options.attempts == 1
    assert api_client._http_options.timeout == 4000


def test_both_production_client_factories_disable_sdk_retries() -> None:
    """One Run must mean one provider request, so SDK retries are disabled explicitly."""
    openai_client = create_openai_client(OPENAI_KEY, 60.0)
    anthropic_client = create_anthropic_client(ANTHROPIC_KEY, 60.0)

    assert openai_client.max_retries == 0
    assert anthropic_client.max_retries == 0
    assert openai_client.timeout is not None
    assert anthropic_client.timeout is not None


def test_both_production_client_factories_pin_the_canonical_provider_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambient SDK variable must not silently redirect a canonical provider.

    Both official SDKs otherwise resolve their base URL from the process environment, so an
    unrelated ``OPENAI_BASE_URL`` or ``ANTHROPIC_BASE_URL`` could send a Run intended for the
    canonical provider to an OpenAI-compatible or proxy endpoint instead.
    """
    monkeypatch.setenv("OPENAI_BASE_URL", "https://redirect.invalid/v1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://redirect.invalid")

    openai_client = create_openai_client(OPENAI_KEY, 60.0)
    anthropic_client = create_anthropic_client(ANTHROPIC_KEY, 60.0)

    assert str(openai_client.base_url).rstrip("/") == "https://api.openai.com/v1"
    assert str(anthropic_client.base_url).rstrip("/") == "https://api.anthropic.com"


def test_composition_builds_clients_locally_without_a_network_request() -> None:
    """Client construction is local and must not contact either provider."""
    composed = composition.compose_model_providers(ANTHROPIC_KEY, OPENAI_KEY)

    assert composed.openai_client is not None
    assert composed.anthropic_client is not None
    assert composed.catalog.is_configured(OPENAI_ID)
    assert composed.catalog.is_configured(ANTHROPIC_ID)
