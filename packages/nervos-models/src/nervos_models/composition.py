"""Provider composition owned by the concrete model-adapter package."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from anthropic import AsyncAnthropic
from google import genai
from nervos_core.application.model_completion import ModelCompletion
from nervos_core.application.model_providers import ModelProviderCatalog
from openai import AsyncOpenAI

from nervos_models.anthropic import (
    PROVIDER_ID as ANTHROPIC_PROVIDER_ID,
)
from nervos_models.anthropic import (
    AnthropicModelCompletion,
    create_anthropic_client,
)
from nervos_models.gemini import (
    PROVIDER_ID as GEMINI_PROVIDER_ID,
)
from nervos_models.gemini import (
    GeminiModelCompletion,
    create_gemini_client,
)
from nervos_models.openai import (
    PROVIDER_ID as OPENAI_PROVIDER_ID,
)
from nervos_models.openai import (
    OpenAIModelCompletion,
    create_openai_client,
)

_CLIENT_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ModelProviderComposition:
    """Configured catalog plus the optional owned client that needs async shutdown."""

    catalog: ModelProviderCatalog
    anthropic_client: AsyncAnthropic | None
    openai_client: AsyncOpenAI | None
    gemini_client: genai.Client | None = None


async def close_model_providers(composition: ModelProviderComposition) -> None:
    """Close configured provider clients without exposing credentials to the API."""
    if composition.anthropic_client is not None:
        await composition.anthropic_client.close()
    if composition.openai_client is not None:
        await composition.openai_client.close()
    if composition.gemini_client is not None:
        await composition.gemini_client.aio.aclose()
        composition.gemini_client.close()


def compose_model_providers(
    anthropic_api_key: str | None,
    openai_api_key: str | None = None,
    gemini_api_key: str | None = None,
) -> ModelProviderComposition:
    """Build all known providers locally, without a network request."""
    entries: list[tuple[str, Callable[[], ModelCompletion]]] = []
    anthropic_client: AsyncAnthropic | None = None
    openai_client: AsyncOpenAI | None = None
    gemini_client: genai.Client | None = None
    if anthropic_api_key is not None:
        anthropic_client = create_anthropic_client(anthropic_api_key, _CLIENT_TIMEOUT_SECONDS)
        anthropic_completion = AnthropicModelCompletion(anthropic_client)
        entries.append((ANTHROPIC_PROVIDER_ID, lambda: anthropic_completion))
    if openai_api_key is not None:
        openai_client = create_openai_client(openai_api_key, _CLIENT_TIMEOUT_SECONDS)
        openai_completion = OpenAIModelCompletion(openai_client)
        entries.append((OPENAI_PROVIDER_ID, lambda: openai_completion))
    if gemini_api_key is not None:
        gemini_client = create_gemini_client(gemini_api_key, _CLIENT_TIMEOUT_SECONDS)
        gemini_completion = GeminiModelCompletion(gemini_client)
        entries.append((GEMINI_PROVIDER_ID, lambda: gemini_completion))
    return ModelProviderComposition(
        ModelProviderCatalog(
            entries,
            known=[ANTHROPIC_PROVIDER_ID, OPENAI_PROVIDER_ID, GEMINI_PROVIDER_ID],
        ),
        anthropic_client,
        openai_client,
        gemini_client,
    )
