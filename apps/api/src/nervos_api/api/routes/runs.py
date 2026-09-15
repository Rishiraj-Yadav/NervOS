"""Owner-scoped Run resources and the single canonical durable-submission endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from nervos_api.api.dependencies import (
    AgentServiceDependency,
    CurrentUserDependency,
    OriginDependency,
    RunSubmissionDependency,
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
    status_code=status.HTTP_202_ACCEPTED,
)
def create_run(
    agent_instance_id: int,
    body: RunCreateRequest,
    response: Response,
    origin: OriginDependency,
    user: CurrentUserDependency,
    submission: RunSubmissionDependency,
) -> RunResponse:
    """Durably accept one Run and return it as `created`, without executing anything.

    This is the only way a Run is created. The control plane validates structure, commits the
    Run with its Job and initial lifecycle events in one transaction, and returns. No model
    call happens in this process, because this process composes no executor and no credential.

    The status describes the HTTP resource operation, never the model outcome: a Run that a
    Worker later executes and records as `failed` still returned 202. Acceptance and execution
    failure are different things, and conflating them would make a durable queue unrepresentable.
    """
    del origin
    run = submission.submit_run(user.id, agent_instance_id, body.input)
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
