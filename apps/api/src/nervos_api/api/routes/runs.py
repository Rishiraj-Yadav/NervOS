"""Owner-scoped Run resources and the single canonical durable-submission endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from nervos_api.api.dependencies import (
    AgentServiceDependency,
    CurrentUserDependency,
    OriginDependency,
    RunCancellationDependency,
    RunSubmissionDependency,
)
from nervos_api.api.routes.agent_instances import next_before_id
from nervos_api.api.schemas import (
    RunCreateRequest,
    RunEventPageResponse,
    RunEventResponse,
    RunPageResponse,
    RunResponse,
)

router = APIRouter()

PageLimit = Annotated[int, Query(ge=1, le=50)]
BeforeId = Annotated[int | None, Query(gt=0)]
# A Run accumulates a legitimate history where a Run *page* does not, so one Event page is wider
# than one Run page: a Run that retried twice produces roughly twenty Events and should drain in a
# round trip or two. The upper bound is what keeps any one response small.
EventPageLimit = Annotated[int, Query(ge=1, le=200)]
AfterSequence = Annotated[int, Query(ge=0)]


def next_after_sequence(sequences: list[int], limit: int) -> int | None:
    """Return the cursor for the next Event page when a further page may exist.

    The same rule as `next_before_id`: a full page may or may not be the last one, so it yields a
    cursor, and the loop ends on the first short page. A history that ends exactly on a page
    boundary therefore costs one further empty request to prove it is drained. That is preferred to
    a count query, which would answer a question nobody asked and could not be made atomic with the
    page it was asked about anyway.
    """
    return sequences[-1] if len(sequences) == limit else None


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


@router.get("/runs/{run_id}/events", response_model=RunEventPageResponse)
def list_run_events(
    run_id: int,
    user: CurrentUserDependency,
    service: AgentServiceDependency,
    limit: EventPageLimit = 50,
    after_sequence: AfterSequence = 0,
) -> RunEventPageResponse:
    """Return one ascending-sequence page of an owned Run's durable execution timeline.

    This is the Run's own sub-resource, so it reuses the Run's ownership rule exactly: a foreign id
    and a nonexistent one produce the same 404 with the same body, and the timeline can therefore
    never be used to probe whether another user's Run exists.

    `sequence` is the order and the cursor. It is allocated contiguously from a per-Run
    high-water mark inside the writing transaction, so a batch is committed atomically and a reader
    always sees a whole batch or none of it -- which is why a client that has applied everything up
    to sequence N can never skip an Event, and why `created_at` is display metadata only.

    Observability only: this reads. It cannot claim, start, retry, cancel, or execute anything, and
    it exposes no Job, Attempt, claim token, worker identity, or scheduling state.
    """
    events = service.list_run_events(user.id, run_id, after_sequence, limit)
    return RunEventPageResponse(
        items=[RunEventResponse.from_domain(event) for event in events],
        next_after_sequence=next_after_sequence([event.sequence for event in events], limit),
    )


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
def cancel_run(
    run_id: int,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: RunCancellationDependency,
) -> RunResponse:
    """Durably cancel one owned Run and return the resulting Run.

    Cancellation is authoritative: the durable transition commits inside this request, so it
    completes whether or not a Worker is running, and the owning Worker discovers the revoked
    authority on its next heartbeat. It is idempotent — cancelling an already-cancelled Run
    returns the same Run, appends no second event, and rewrites neither the original finish
    instant nor the original elapsed interval.

    A Run that already succeeded or failed is history and is never rewritten: that is a 409,
    not a silent conversion. The response never exposes a Job, Attempt, claim token, worker id,
    or retry disposition; the public contract is the Run alone.

    Cancelling stops *NervOS* from waiting for and persisting a result. It does not and cannot
    prove that the remote provider stopped processing a request it already received, so this
    endpoint claims no remote cancellation, no billing stop, and no remote rollback.
    """
    del origin
    return RunResponse.from_domain(service.cancel_run(user.id, run_id))
