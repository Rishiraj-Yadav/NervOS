"""Provider composition owned by the concrete model-adapter package."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from anthropic import AsyncAnthropic
from nervos_core.application.model_completion import ModelCompletion
from nervos_core.application.model_providers import ModelProviderCatalog

from nervos_models.anthropic import PROVIDER_ID, AnthropicModelCompletion, create_anthropic_client

_CLIENT_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ModelProviderComposition:
    """Configured catalog plus the optional owned client that needs async shutdown."""

    catalog: ModelProviderCatalog
    anthropic_client: AsyncAnthropic | None


async def close_model_providers(composition: ModelProviderComposition) -> None:
    """Close the configured provider client without exposing credentials to the API."""
    if composition.anthropic_client is not None:
        await composition.anthropic_client.close()


def compose_model_providers(anthropic_api_key: str | None) -> ModelProviderComposition:
    """Build provider services locally, without a network request.

    Credential presence and provider registration remain distinct. Anthropic is always a
    known provider, while an absent credential leaves it unavailable during execution
    preflight.
    """
    entries: list[tuple[str, Callable[[], ModelCompletion]]] = []
    client: AsyncAnthropic | None = None
    if anthropic_api_key is not None:
        client = create_anthropic_client(anthropic_api_key, _CLIENT_TIMEOUT_SECONDS)
        completion = AnthropicModelCompletion(client)
        entries.append((PROVIDER_ID, lambda: completion))
    return ModelProviderComposition(
        ModelProviderCatalog(entries, known=[PROVIDER_ID]),
        client,
    )
