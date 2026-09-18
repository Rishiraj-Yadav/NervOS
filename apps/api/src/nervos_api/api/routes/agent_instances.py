"""Owner-scoped Agent Instance resources for the trusted Chat surface."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status
from nervos_core.application.model_providers import ModelProviderCatalog, UnknownModelProvider
from nervos_core.application.trusted_chat import CHAT_DEFINITION_ID, CHAT_TOOL_DEFINITION_ID
from nervos_core.domain.agents import AgentDefinitionId, validate_model_provider

from nervos_api.api.dependencies import (
    AgentServiceDependency,
    CurrentUserDependency,
    ModelProviderCatalogDependency,
    OriginDependency,
)
from nervos_api.api.errors import UnsupportedB3AgentDefinition
from nervos_api.api.schemas import (
    AgentInstanceCreateRequest,
    AgentInstancePageResponse,
    AgentInstanceResponse,
    AgentInstanceUpdateRequest,
)

router = APIRouter(prefix="/agent-instances")

# Keyset pagination shared with the accepted B1 persistence semantics.
PageLimit = Annotated[int, Query(ge=1, le=50)]
BeforeId = Annotated[int | None, Query(gt=0)]


# The exact trusted definition identities this milestone exposes. It is an allow-list of exact
# identities, never a version range and never "anything the resolver happens to know": a definition
# registered by a later milestone does not become creatable here by being registered. D4 admits the
# tool-enabled Chat definition beside the unchanged tool-free one.
TRUSTED_DEFINITION_IDS = frozenset({CHAT_DEFINITION_ID, CHAT_TOOL_DEFINITION_ID})


def require_trusted_definition(agent_key: str, agent_definition_version: str) -> AgentDefinitionId:
    """Return the exact trusted definition identity, or fail closed.

    A malformed identity raises `InvalidAgentDefinitionId`, which maps to a safe 422. A
    structurally valid identity outside this milestone's trusted set is rejected with
    `UnsupportedB3AgentDefinition`, so a definition registered by a later milestone can never
    become creatable here simply because the resolver knows about it.
    """
    requested = AgentDefinitionId(agent_key, agent_definition_version)
    if requested not in TRUSTED_DEFINITION_IDS:
        raise UnsupportedB3AgentDefinition
    return requested


def require_known_provider(model_provider: str, catalog: ModelProviderCatalog) -> None:
    """Reject a provider identifier that is malformed, then one this process does not know.

    Structural validity is the domain's rule, so it is reused rather than restated: a blank or
    malformed identifier is a configuration error, while a well-formed but unrecognized one is
    an unsupported provider. A known-but-unconfigured provider stays acceptable, because absence
    of a process credential is a pre-run execution concern rather than a configuration error.
    """
    validate_model_provider(model_provider)
    if not catalog.is_known(model_provider):
        raise UnknownModelProvider


def next_before_id(ids: list[int], limit: int) -> int | None:
    """Return the cursor for the next page when a further page may exist."""
    return ids[-1] if len(ids) == limit else None


@router.get("", response_model=AgentInstancePageResponse)
def list_agent_instances(
    user: CurrentUserDependency,
    service: AgentServiceDependency,
    limit: PageLimit = 20,
    before_id: BeforeId = None,
) -> AgentInstancePageResponse:
    """Return one newest-first page of Agent Instances owned by the authenticated user."""
    instances = service.list_instances(user.id, limit, before_id)
    return AgentInstancePageResponse(
        items=[AgentInstanceResponse.from_domain(instance) for instance in instances],
        next_before_id=next_before_id([instance.id for instance in instances], limit),
    )


@router.post("", response_model=AgentInstanceResponse, status_code=status.HTTP_201_CREATED)
def create_agent_instance(
    body: AgentInstanceCreateRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: AgentServiceDependency,
    catalog: ModelProviderCatalogDependency,
) -> AgentInstanceResponse:
    """Explicitly create one owned Agent Instance; nothing is ever created implicitly."""
    del origin
    definition_id = require_trusted_definition(body.agent_key, body.agent_definition_version)
    require_known_provider(body.model_provider, catalog)
    instance = service.create_instance(
        user.id,
        definition_id,
        body.display_name,
        body.model_provider,
        body.model_name,
        enabled=body.enabled,
    )
    return AgentInstanceResponse.from_domain(instance)


@router.get("/{agent_instance_id}", response_model=AgentInstanceResponse)
def get_agent_instance(
    agent_instance_id: int,
    user: CurrentUserDependency,
    service: AgentServiceDependency,
) -> AgentInstanceResponse:
    """Return one owned Agent Instance; foreign and nonexistent ids are indistinguishable."""
    return AgentInstanceResponse.from_domain(service.get_instance(user.id, agent_instance_id))


@router.patch("/{agent_instance_id}", response_model=AgentInstanceResponse)
def update_agent_instance(
    agent_instance_id: int,
    body: AgentInstanceUpdateRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: AgentServiceDependency,
    catalog: ModelProviderCatalogDependency,
) -> AgentInstanceResponse:
    """Apply exactly one committed mutation: the configuration triple, or the enabled toggle."""
    del origin
    if body.enabled is not None:
        # Shape B: the toggle alone. It never touches configuration, and configuration never
        # touches it, so a single request can never partially succeed.
        return AgentInstanceResponse.from_domain(
            service.set_enabled(user.id, agent_instance_id, body.enabled_toggle())
        )

    # Shape A: all three configuration values are supplied verbatim. They are never merged
    # with the stored values, so an explicitly empty value reaches domain validation and fails
    # rather than being silently replaced by what was already stored.
    display_name, model_provider, model_name = body.configuration_triple()
    require_known_provider(model_provider, catalog)
    return AgentInstanceResponse.from_domain(
        service.update_instance(
            user.id, agent_instance_id, display_name, model_provider, model_name
        )
    )
