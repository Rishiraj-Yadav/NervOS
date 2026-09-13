"""Owner-scoped Run resources and the single canonical execution endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from nervos_api.api.dependencies import (
    AgentServiceDependency,
    CurrentUserDependency,
    OriginDependency,
    RunCoordinatorDependency,
)
from nervos_api.api.routes.agent_instances import next_before_id
from nervos_api.api.schemas import (
    RunCreateRequest,
    RunPageResponse,
    RunResponse,
)

router = APIRouter()

PageLimit = Annotated[int, Query(ge=1, le=50)]
BeforeId = Annotated[int | None, Query(gt=0)]


@router.post(
    "/agent-instances/{agent_instance_id}/runs",
    response_model=RunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_run(
    agent_instance_id: int,
    body: RunCreateRequest,
    response: Response,
    origin: OriginDependency,
    user: CurrentUserDependency,
    coordinator: RunCoordinatorDependency,
) -> RunResponse:
    """Execute one bounded one-shot Run and return the persisted terminal Run.

    This is the only execution path in NervOS. It delegates entirely to the accepted B2
    coordinator, which owns preflight, Run creation, the single provider call, and terminal
    persistence. The route adds no lifecycle behavior of its own.

    The status describes the HTTP resource operation, never the model outcome: a Run that
    executed and was durably recorded as `failed` still returns 201 with that Run.
    """
    del origin
    run = await coordinator.execute(user.id, agent_instance_id, body.input)
    response.headers["Location"] = f"/api/v1/runs/{run.id}"
    return RunResponse.from_domain(run)


@router.get("/agent-instances/{agent_instance_id}/runs", response_model=RunPageResponse)
def list_runs(
    agent_instance_id: int,
    user: CurrentUserDependency,
    service: AgentServiceDependency,
    limit: PageLimit = 20,
    before_id: BeforeId = None,
) -> RunPageResponse:
    """Return one newest-first page of Runs belonging to an owned Agent Instance.

    A nonexistent Agent Instance and a foreign one are the same 404 `agent_instance_not_found`,
    so run history can never be used to probe whether another user's instance exists.
    """
    runs = service.list_runs(user.id, agent_instance_id, limit, before_id)
    return RunPageResponse(
        items=[RunResponse.from_domain(run) for run in runs],
        next_before_id=next_before_id([run.id for run in runs], limit),
    )


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(
    run_id: int,
    user: CurrentUserDependency,
    service: AgentServiceDependency,
) -> RunResponse:
    """Return one owned Run; foreign and nonexistent ids are indistinguishable."""
    return RunResponse.from_domain(service.get_run(user.id, run_id))
