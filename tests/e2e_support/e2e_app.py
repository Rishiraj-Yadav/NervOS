"""Test-only ASGI factory for the offline two-provider browser journey."""

from __future__ import annotations

from fastapi import FastAPI
from nervos_api.api.dependencies import utc_now
from nervos_api.app import create_app as create_production_app
from nervos_core.application.agent_definitions import create_builtin_definition_registry
from nervos_core.application.agents import AgentService
from nervos_core.application.model_completion import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
)
from nervos_core.application.model_providers import ModelProviderCatalog
from nervos_core.application.run_coordinator import RunCoordinator
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence

ANTHROPIC_ID = "anthropic"
OPENAI_ID = "openai"
ANTHROPIC_REPLY = "Deterministic Anthropic reply from NervOS."
OPENAI_REPLY = "Deterministic OpenAI reply from NervOS."


class DeterministicCompletion:
    """Provider-identifying offline completion used only by supervised E2E."""

    def __init__(self, provider_id: str, reply: str, total_tokens: int | None) -> None:
        self.provider_id = provider_id
        self.reply = reply
        self.total_tokens = total_tokens
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            self.reply,
            self.provider_id,
            request.model_name,
            StopOutcome.STOP,
            ModelUsage(11, 7, self.total_tokens),
        )


def create_app() -> FastAPI:
    """Compose the real API, then replace both providers with distinct offline fakes."""
    app = create_production_app()
    composed_catalog = app.state.model_provider_catalog
    handlers = create_builtin_handler_registry()
    anthropic = DeterministicCompletion(ANTHROPIC_ID, ANTHROPIC_REPLY, None)
    openai = DeterministicCompletion(OPENAI_ID, OPENAI_REPLY, 18)
    catalog = ModelProviderCatalog(
        [(ANTHROPIC_ID, lambda: anthropic), (OPENAI_ID, lambda: openai)],
        known=[ANTHROPIC_ID, OPENAI_ID],
    )
    service = AgentService(
        SqlAlchemyAgentPersistence(app.state.session_factory),
        create_builtin_definition_registry(),
        utc_now,
        handlers,
        catalog,
    )
    app.state.model_provider_catalog = catalog
    app.state.agent_service = service
    app.state.run_coordinator = RunCoordinator(service, handlers, catalog)

    if composed_catalog is catalog:
        raise RuntimeError("production composition already held the deterministic catalog")
    if any(composed_catalog.is_configured(identifier) for identifier in (ANTHROPIC_ID, OPENAI_ID)):
        raise RuntimeError("production composition had a configured provider credential")
    if catalog.resolve(ANTHROPIC_ID) is not anthropic or catalog.resolve(OPENAI_ID) is not openai:
        raise RuntimeError("deterministic provider catalog was not installed")
    if anthropic is openai:
        raise RuntimeError("deterministic providers must be distinct")
    return app
