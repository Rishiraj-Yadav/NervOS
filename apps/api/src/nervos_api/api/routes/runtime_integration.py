"""Thin, authenticated product controls over runtime integration application services."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from nervos_api.api.dependencies import (
    CurrentUserDependency,
    OriginDependency,
    RuntimeIntegrationDependency,
)

router = APIRouter()
Positive = Annotated[int, Query(gt=0)]


class BindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*$")
    definition_id: int = Field(gt=0)


class PermissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition_id: int = Field(gt=0)
    action: Literal["grant", "revoke", "reconfirm"]


class MemoryPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["manual", "review", "automatic_private"]
    extraction_enabled: bool = False
    expected_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_extraction(self) -> "MemoryPolicyRequest":
        if self.mode == "manual" and self.extraction_enabled:
            raise ValueError("manual mode cannot enable automatic extraction")
        return self


class SuggestionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool


@router.get("/tools", response_model=dict[str, Any])
def tools(
    user: CurrentUserDependency,
    service: RuntimeIntegrationDependency,
    connection_id: Positive | None = None,
    before_id: Positive | None = None,
) -> dict[str, Any]:
    return service.tools(user.id, connection_id, before_id)


@router.get("/agent-instances/{instance_id}/tools", response_model=dict[str, Any])
def agent_tools(
    instance_id: int, user: CurrentUserDependency, service: RuntimeIntegrationDependency
) -> dict[str, Any]:
    return service.agent_tools(user.id, instance_id)


@router.post("/agent-instances/{instance_id}/tool-bindings", status_code=204)
def bind(
    instance_id: int,
    body: BindingRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: RuntimeIntegrationDependency,
) -> Response:
    del origin
    service.bind_tool(user.id, instance_id, body.alias, body.definition_id)
    return Response(status_code=204)


@router.delete("/agent-instances/{instance_id}/tool-bindings/{alias}", status_code=204)
def unbind(
    instance_id: int,
    alias: str,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: RuntimeIntegrationDependency,
) -> Response:
    del origin
    service.unbind_tool(user.id, instance_id, alias)
    return Response(status_code=204)


@router.post("/agent-instances/{instance_id}/tool-permissions", status_code=204)
def permission(
    instance_id: int,
    body: PermissionRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: RuntimeIntegrationDependency,
) -> Response:
    del origin
    service.grant_tool(user.id, instance_id, body.definition_id, body.action)
    return Response(status_code=204)


@router.get("/agent-instances/{instance_id}/memory-policy", response_model=dict[str, Any])
def policy(
    instance_id: int, user: CurrentUserDependency, service: RuntimeIntegrationDependency
) -> dict[str, Any]:
    return service.policy(user.id, instance_id)


@router.patch("/agent-instances/{instance_id}/memory-policy", response_model=dict[str, Any])
def update_policy(
    instance_id: int,
    body: MemoryPolicyRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: RuntimeIntegrationDependency,
) -> dict[str, Any]:
    del origin
    return service.set_policy(
        user.id, instance_id, body.mode, body.extraction_enabled, body.expected_revision
    )


@router.get("/memory-suggestions", response_model=dict[str, Any])
def suggestions(
    user: CurrentUserDependency,
    service: RuntimeIntegrationDependency,
    agent_instance_id: Positive | None = None,
    before_id: Positive | None = None,
) -> dict[str, Any]:
    return service.suggestions(user.id, agent_instance_id, before_id)


@router.post("/memory-suggestions/{suggestion_id}/decision", status_code=204)
def decide(
    suggestion_id: int,
    body: SuggestionDecision,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: RuntimeIntegrationDependency,
) -> Response:
    del origin
    service.decide_suggestion(user.id, suggestion_id, body.approve)
    return Response(status_code=204)


@router.get("/runs/{run_id}/context", response_model=dict[str, Any])
def context(
    run_id: int, user: CurrentUserDependency, service: RuntimeIntegrationDependency
) -> dict[str, Any]:
    return service.run_context(user.id, run_id)


@router.get("/runtime-health", response_model=dict[str, Any])
def health(user: CurrentUserDependency, service: RuntimeIntegrationDependency) -> dict[str, Any]:
    return service.health(user.id)
