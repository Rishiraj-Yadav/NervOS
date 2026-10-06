"""Thin, authenticated product controls over durable workflows (ADR 0039, W5b).

Every route is owner-scoped from the session, never from a body or query field. The routes
hold no business logic: they validate a request shape, call the application service, and
return the service's already-safe projection. Nothing here decides a transition, and
nothing here can make a workflow advance -- that is the Scheduler's bounded tick.

The projections are deliberately built from the domain's owner-safe views rather than from
raw rows, so a checkpoint's contents never reach the browser by accident: the UI receives a
bounded summary and a byte count, not application state the owner did not put there.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from nervos_api.api.dependencies import CurrentUserDependency, WorkflowsDependency

router = APIRouter(prefix="/workflows")
Positive = Annotated[int, Query(gt=0)]


@router.get("/{workflow_id}/checkpoints/{revision}", response_model=dict[str, Any])
def inspect_checkpoint(
    workflow_id: int, revision: int, user: CurrentUserDependency, service: WorkflowsDependency
) -> dict[str, Any]:
    return service.checkpoint(user.id, workflow_id, revision)


class CreateWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_instance_id: int = Field(gt=0)
    submission_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    input_text: str = Field(min_length=1, max_length=8000)
    workflow_kind: str = Field(default="research", min_length=1, max_length=64)


class PauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool


class SignalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    payload: dict[str, Any] = Field(default_factory=dict)
    # Required, not optional. A signal delivered against a stale revision would be accepted
    # against a state the caller never saw, which is exactly the ambiguity the revision
    # exists to prevent.
    expected_revision: int = Field(ge=0)


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    # Required. An owner decision recorded against a revision they never saw would be
    # authorizing different work than the one displayed.
    expected_revision: int = Field(ge=0)


@router.get("", response_model=dict[str, Any])
def list_workflows(
    user: CurrentUserDependency,
    service: WorkflowsDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    before_id: Positive | None = None,
) -> dict[str, Any]:
    return service.list_workflows(user.id, limit=limit, before_id=before_id)


@router.post("", status_code=201, response_model=dict[str, Any])
def create_workflow(
    body: CreateWorkflowRequest, user: CurrentUserDependency, service: WorkflowsDependency
) -> dict[str, Any]:
    return service.create_workflow(
        owner_user_id=user.id,
        agent_instance_id=body.agent_instance_id,
        submission_key=body.submission_key,
        input_text=body.input_text,
        workflow_kind=body.workflow_kind,
    )


@router.get("/{workflow_id}", response_model=dict[str, Any])
def get_workflow(
    workflow_id: int, user: CurrentUserDependency, service: WorkflowsDependency
) -> dict[str, Any]:
    return service.workflow_detail(user.id, workflow_id)


@router.post("/{workflow_id}/pause", response_model=dict[str, Any])
def pause_workflow(
    workflow_id: int,
    body: PauseRequest,
    user: CurrentUserDependency,
    service: WorkflowsDependency,
) -> dict[str, Any]:
    # Pausing stops *future* steps. It is deliberately a separate verb from cancel, which
    # terminates the workflow: the UI must not be able to express "stop the next step" and
    # "abandon this" as the same action.
    return service.set_paused(user.id, workflow_id, body.paused)


@router.post("/{workflow_id}/cancel", response_model=dict[str, Any])
def cancel_workflow(
    workflow_id: int, user: CurrentUserDependency, service: WorkflowsDependency
) -> dict[str, Any]:
    return service.cancel_workflow(user.id, workflow_id)


@router.post("/{workflow_id}/signals", status_code=202, response_model=dict[str, Any])
def deliver_signal(
    workflow_id: int,
    body: SignalRequest,
    user: CurrentUserDependency,
    service: WorkflowsDependency,
) -> dict[str, Any]:
    return service.deliver_signal(
        owner_user_id=user.id,
        workflow_id=workflow_id,
        signal_key=body.signal_key,
        payload=body.payload,
        expected_revision=body.expected_revision,
    )


@router.get("/{workflow_id}/decisions", response_model=dict[str, Any])
def list_decisions(
    workflow_id: int, user: CurrentUserDependency, service: WorkflowsDependency
) -> dict[str, Any]:
    return service.workflow_decisions(user.id, workflow_id)


@router.post("/{workflow_id}/decisions/{decision_id}", response_model=dict[str, Any])
def decide(
    workflow_id: int,
    decision_id: int,
    body: DecisionRequest,
    user: CurrentUserDependency,
    service: WorkflowsDependency,
) -> dict[str, Any]:
    # This is an owner decision about *proposed work*. It is not the H3 live-dispatch
    # approval: approving here authorizes one step to proceed to its own dispatch boundary,
    # where live grants, account state and cancellation are rechecked.
    return service.decide(
        owner_user_id=user.id,
        workflow_id=workflow_id,
        decision_id=decision_id,
        approve=body.approve,
        expected_revision=body.expected_revision,
    )


@router.get("/{workflow_id}/needs-review", response_model=dict[str, Any])
def needs_review(
    workflow_id: int, user: CurrentUserDependency, service: WorkflowsDependency
) -> dict[str, Any]:
    """Safe recovery guidance for a workflow the runtime cannot advance on its own."""
    return service.workflow_recovery(user.id, workflow_id)
