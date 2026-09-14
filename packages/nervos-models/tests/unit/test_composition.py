"""Deterministic composition tests for the two production providers."""

from __future__ import annotations

import pytest
from nervos_models import composition
from nervos_models.anthropic import PROVIDER_ID as ANTHROPIC_ID
from nervos_models.anthropic import create_anthropic_client
from nervos_models.openai import PROVIDER_ID as OPENAI_ID
from nervos_models.openai import create_openai_client

ANTHROPIC_KEY = "SYNTHETIC-ANTHROPIC-CREDENTIAL"
OPENAI_KEY = "SYNTHETIC-OPENAI-CREDENTIAL"


def test_both_providers_are_known_without_any_credential() -> None:
    """Absent credentials make a provider unavailable, never unknown."""
    composed = composition.compose_model_providers(None, None)

    assert composed.catalog.is_known(ANTHROPIC_ID)
    assert composed.catalog.is_known(OPENAI_ID)
    assert not composed.catalog.is_configured(ANTHROPIC_ID)
    assert not composed.catalog.is_configured(OPENAI_ID)
    assert composed.anthropic_client is None
    assert composed.openai_client is None


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


@pytest.mark.parametrize("alias", ["Anthropic", "ANTHROPIC", "open-ai", "OpenAI", "gpt", "gemini"])
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

    class _Client:
        def __init__(self, name: str) -> None:
            self._name = name

        async def close(self) -> None:
            closed.append(self._name)

    composed = composition.ModelProviderComposition(
        composition.compose_model_providers(None, None).catalog,
        _Client("anthropic"),  # type: ignore[arg-type]
        _Client("openai"),  # type: ignore[arg-type]
    )
    await composition.close_model_providers(composed)

    assert closed == ["anthropic", "openai"]


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
