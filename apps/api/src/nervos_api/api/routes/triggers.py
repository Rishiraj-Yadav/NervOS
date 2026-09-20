"""Owner-scoped trigger management: schedules, webhooks, and internal events.

The control plane decides *what may cause a Run* and observes *what did*. It never executes
anything: creation, editing, enablement, deletion, credential rotation and history are
owner-scoped application-service calls, and every schedule instant is computed by the one E2
evaluator the service composes -- no route performs cron arithmetic.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from nervos_core.domain.triggers import (
    DEFAULT_TIMEZONE,
    InvalidTrigger,
    ScheduleSpec,
    TriggerDefinition,
    TriggerKind,
)

from nervos_api.api.dependencies import (
    AgentServiceDependency,
    CurrentUserDependency,
    OriginDependency,
    TriggerManagementDependency,
)
from nervos_api.api.schemas import (
    IssuedWebhookResponse,
    TriggerCreateRequest,
    TriggerDeletedResponse,
    TriggerDetailResponse,
    TriggerOccurrencePageResponse,
    TriggerOccurrenceResponse,
    TriggerPageResponse,
    TriggerSummaryResponse,
    TriggerUpdateRequest,
)

router = APIRouter(prefix="/triggers")

# Keyset pagination, shared with the accepted Agent Instance semantics.
PageLimit = Annotated[int, Query(ge=1, le=50)]
BeforeId = Annotated[int | None, Query(gt=0)]

_SCHEDULE_FIELDS = ("run_at", "interval_seconds", "cron_expression", "timezone")


def _page(triggers: list[TriggerDefinition], limit: int) -> TriggerPageResponse:
    return TriggerPageResponse(
        items=[TriggerSummaryResponse.from_domain(trigger) for trigger in triggers],
        next_before_id=triggers[-1].id if len(triggers) == limit else None,
    )


def _create_schedule(body: TriggerCreateRequest) -> ScheduleSpec | None:
    """Build the schedule spec a create request carries, or None for a delivery-driven kind."""
    if body.kind == "one_time":
        return ScheduleSpec.one_time(body.run_at)  # type: ignore[arg-type]
    if body.kind == "interval":
        return ScheduleSpec.interval(body.interval_seconds)  # type: ignore[arg-type]
    if body.kind == "cron":
        return ScheduleSpec.cron(body.cron_expression or "", body.timezone or DEFAULT_TIMEZONE)
    return None


def _patch_schedule(kind: TriggerKind, body: TriggerUpdateRequest) -> ScheduleSpec | None:
    """Build the schedule spec one edit carries, or None when the schedule is untouched.

    A schedule edit supplies its kind's complete field set or none of it, so a partial change can
    never silently erase a field; a field that does not belong to the trigger's kind is refused.
    A naive ``run_at`` is refused by the domain when the spec is built.
    """
    supplied = {name for name in _SCHEDULE_FIELDS if getattr(body, name) is not None}
    if kind is TriggerKind.ONE_TIME:
        if not supplied:
            return None
        if supplied != {"run_at"}:
            raise InvalidTrigger("a one_time edit requires run_at and no other schedule field")
        return ScheduleSpec.one_time(body.run_at)  # type: ignore[arg-type]
    if kind is TriggerKind.INTERVAL:
        if not supplied:
            return None
        if supplied != {"interval_seconds"}:
            raise InvalidTrigger("an interval edit requires interval_seconds alone")
        return ScheduleSpec.interval(body.interval_seconds)  # type: ignore[arg-type]
    if kind is TriggerKind.CRON:
        if not supplied:
            return None
        if "run_at" in supplied or "interval_seconds" in supplied:
            raise InvalidTrigger("a cron edit requires cron_expression and timezone alone")
        if "cron_expression" not in supplied:
            raise InvalidTrigger("a cron edit requires cron_expression and timezone alone")
        return ScheduleSpec.cron(body.cron_expression or "", body.timezone or DEFAULT_TIMEZONE)
    if supplied:
        raise InvalidTrigger(f"a {kind.value} trigger carries no schedule")
    return None


@router.get("", response_model=TriggerPageResponse)
def list_triggers(
    user: CurrentUserDependency,
    service: TriggerManagementDependency,
    limit: PageLimit = 20,
    before_id: BeforeId = None,
) -> TriggerPageResponse:
    """Return one newest-first page of triggers owned by the authenticated user."""
    return _page(list(service.list_triggers(user.id, limit=limit, before_id=before_id)), limit)


@router.get("/{trigger_id}", response_model=TriggerDetailResponse)
def get_trigger(
    trigger_id: int,
    user: CurrentUserDependency,
    service: TriggerManagementDependency,
) -> TriggerDetailResponse:
    """Return one owned trigger; foreign and nonexistent ids are indistinguishable."""
    return TriggerDetailResponse.from_domain(service.get_trigger(user.id, trigger_id))


@router.post(
    "",
    response_model=TriggerDetailResponse | IssuedWebhookResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_trigger(
    body: TriggerCreateRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: TriggerManagementDependency,
    agents: AgentServiceDependency,
    response: Response,
) -> TriggerDetailResponse | IssuedWebhookResponse:
    """Create one trigger in its requested enabled state, atomically, in one transaction.

    A webhook create returns the one-time plaintext secret beside the safe representation; every
    other kind returns the safe representation alone. Both shapes are ``Cache-Control: no-store``.
    """
    del origin
    # The target Agent Instance is resolved owner-scoped first, so a foreign or missing target
    # answers with the Agent Instance resource's own not-found code.
    agents.get_instance(user.id, body.agent_instance_id)
    if body.kind == "webhook":
        issued = service.create_webhook(
            user.id,
            agent_instance_id=body.agent_instance_id,
            display_name=body.display_name,
            input_text=body.input_text,
            enabled=body.enabled,
        )
        response.headers["Cache-Control"] = "no-store"
        return IssuedWebhookResponse(
            trigger=TriggerDetailResponse.from_domain(issued.trigger), secret=issued.secret
        )
    trigger = service.create_trigger(
        user.id,
        agent_instance_id=body.agent_instance_id,
        display_name=body.display_name,
        input_text=body.input_text,
        kind=TriggerKind(body.kind),
        schedule=_create_schedule(body),
        event_type=body.event_type,
        enabled=body.enabled,
    )
    return TriggerDetailResponse.from_domain(trigger)


@router.patch("/{trigger_id}", response_model=TriggerDetailResponse)
def update_trigger(
    trigger_id: int,
    body: TriggerUpdateRequest,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: TriggerManagementDependency,
) -> TriggerDetailResponse:
    """Apply one defining edit, guarded by the revision the caller last saw."""
    del origin
    current = service.get_trigger(user.id, trigger_id)
    schedule = _patch_schedule(current.kind, body)
    event_type = body.event_type
    if event_type is not None and current.kind is not TriggerKind.EVENT:
        raise InvalidTrigger(f"a {current.kind.value} trigger carries no event type")
    if current.kind is TriggerKind.EVENT and schedule is not None:
        raise InvalidTrigger("an event trigger carries no schedule")
    return TriggerDetailResponse.from_domain(
        service.update_trigger(
            user.id,
            trigger_id,
            expected_config_revision=body.expected_config_revision,
            display_name=body.display_name,
            input_text=body.input_text,
            schedule=schedule,
            event_type=event_type,
        )
    )


@router.post("/{trigger_id}/enable", response_model=TriggerDetailResponse)
def enable_trigger(
    trigger_id: int,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: TriggerManagementDependency,
) -> TriggerDetailResponse:
    """Enable one owned trigger; a schedule's first future fire
    time is computed by E2's evaluator."""
    del origin
    return TriggerDetailResponse.from_domain(service.set_enabled(user.id, trigger_id, True))


@router.post("/{trigger_id}/disable", response_model=TriggerDetailResponse)
def disable_trigger(
    trigger_id: int,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: TriggerManagementDependency,
) -> TriggerDetailResponse:
    """Disable one owned trigger, clearing any next fire time in the same transaction."""
    del origin
    return TriggerDetailResponse.from_domain(service.set_enabled(user.id, trigger_id, False))


@router.delete("/{trigger_id}", response_model=TriggerDeletedResponse)
def delete_trigger(
    trigger_id: int,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: TriggerManagementDependency,
) -> TriggerDeletedResponse:
    """Delete one owned trigger that has no occurrence history."""
    del origin
    service.delete_trigger(user.id, trigger_id)
    return TriggerDeletedResponse()


@router.post("/{trigger_id}/rotate-secret", response_model=IssuedWebhookResponse)
def rotate_webhook_secret(
    trigger_id: int,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: TriggerManagementDependency,
    response: Response,
) -> IssuedWebhookResponse:
    """Rotate a webhook trigger's credential; the new plaintext is returned exactly once."""
    del origin
    issued = service.rotate_webhook_secret(user.id, trigger_id)
    response.headers["Cache-Control"] = "no-store"
    return IssuedWebhookResponse(
        trigger=TriggerDetailResponse.from_domain(issued.trigger), secret=issued.secret
    )


@router.get("/{trigger_id}/occurrences", response_model=TriggerOccurrencePageResponse)
def list_trigger_occurrences(
    trigger_id: int,
    user: CurrentUserDependency,
    service: TriggerManagementDependency,
    limit: PageLimit = 20,
    before_id: BeforeId = None,
) -> TriggerOccurrencePageResponse:
    """Return one newest-first page of one owned trigger's materialization history."""
    occurrences = service.list_occurrences(user.id, trigger_id, limit=limit, before_id=before_id)
    return TriggerOccurrencePageResponse(
        items=[TriggerOccurrenceResponse.from_domain(occurrence) for occurrence in occurrences],
        next_before_id=occurrences[-1].id if len(occurrences) == limit else None,
    )
