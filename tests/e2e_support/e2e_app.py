"""Test-only ASGI factory for the deterministic browser journey.

This module lives outside every shipped package on purpose. It is reachable only by pointing a
test process at it explicitly; there is no environment variable, settings field, or route that can
install it. Production composition (`nervos_api.main:app`) is untouched.

The factory replaces the composed provider catalog with one whose only entry is an offline
deterministic completion, so the browser journey can exercise the real API, migrations, session
handling, coordinator, and Run persistence while making no provider network request.
"""

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

PROVIDER_ID = "anthropic"

# Stable, deterministic answers the browser journey can assert on.
PRIMARY_REPLY = "Deterministic Chat reply from the NervOS test provider."
SECOND_REPLY = "Second independent deterministic reply."


class DeterministicCompletion:
    """Offline provider double used only by the supervised browser journey."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        text = PRIMARY_REPLY if self.calls == 1 else SECOND_REPLY
        return ModelResponse(
            text,
            PROVIDER_ID,
            request.model_name,
            StopOutcome.STOP,
            ModelUsage(11, 7, None),
        )


def create_app() -> FastAPI:
    """Compose the real API and install the deterministic provider in place of the real one."""
    app = create_production_app()
    # Captured before replacement so the fail-closed assertion below compares two genuinely
    # different objects rather than the value assigned on the previous line.
    composed_catalog = app.state.model_provider_catalog
    handlers = create_builtin_handler_registry()
    completion = DeterministicCompletion()
    catalog = ModelProviderCatalog([(PROVIDER_ID, lambda: completion)], known=[PROVIDER_ID])
    service = AgentService(
        SqlAlchemyAgentPersistence(app.state.session_factory),
        create_builtin_definition_registry(),
        utc_now,
        handlers,
        catalog,
    )
    coordinator = RunCoordinator(service, handlers, catalog)
    app.state.model_provider_catalog = catalog
    app.state.agent_service = service
    app.state.run_coordinator = coordinator

    # Fail closed rather than silently falling back to the production composition. These compare
    # genuinely independent objects, and the second one fails if a provider credential ever reaches
    # this process — which the supervisor prevents by stripping it from the child environment.
    if composed_catalog is catalog:
        raise RuntimeError("production composition already held the deterministic catalog")
    if composed_catalog.is_configured(PROVIDER_ID):
        raise RuntimeError("the production composition had a configured provider credential")
    if catalog.resolve(PROVIDER_ID) is not completion:
        raise RuntimeError("the deterministic completion is not the catalog's provider")
    return app
