"""Application read pass-through tests for the B3 Agent Instance and Run resources."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from nervos_core.application.agent_definitions import create_builtin_definition_registry
from nervos_core.application.agents import (
    EVENT_PAGE_LIMIT_MAX,
    AgentInstanceNotFound,
    AgentService,
    InstanceConfiguration,
    RunNotFound,
)
from nervos_core.domain.agents import AgentDefinitionId, AgentInstance
from nervos_core.domain.jobs import RunEvent, RunEventType
from nervos_core.domain.runs import STAGE_B_LIMITS, ModelUsage, Run, RunStatus

NOW = datetime(2026, 1, 1, tzinfo=UTC)
IDENTITY = AgentDefinitionId("nervos.chat", "1")


def instance(instance_id: int = 1, owner_user_id: int = 1) -> AgentInstance:
    return AgentInstance(
        instance_id,
        owner_user_id,
        IDENTITY,
        "Chat",
        True,
        "anthropic",
        "opaque/model",
        NOW,
        NOW,
    )


def run(run_id: int = 1, instance_id: int = 1) -> Run:
    return Run(
        run_id,
        instance_id,
        IDENTITY.agent_key,
        IDENTITY.agent_definition_version,
        "anthropic",
        "opaque/model",
        "hello",
        STAGE_B_LIMITS,
        RunStatus.CREATED,
        NOW,
    )


class RecordingPersistence:
    """Read-only persistence double recording every delegation verbatim.

    The write half of the `AgentPersistence` Protocol is implemented as a loud refusal: these
    tests prove the read pass-throughs delegate without re-deriving anything, so any write
    reaching this double would mean the read path had grown behaviour it must not have. The
    Run lifecycle writes no longer exist on this protocol at all: execution is the Worker's,
    through the fenced execution persistence, so there is nothing here to refuse.
    """

    def __init__(
        self,
        *,
        instances: tuple[AgentInstance, ...] = (),
        runs: tuple[Run, ...] = (),
        events: tuple[RunEvent, ...] = (),
    ):
        self.instances = instances
        self.runs = runs
        self.events = events
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.get_instance_error: type[Exception] | None = None
        self.get_run_error: type[Exception] | None = None

    def get_instance(self, owner_user_id: int, instance_id: int) -> AgentInstance:
        self.calls.append(("get_instance", (owner_user_id, instance_id)))
        if self.get_instance_error is not None:
            raise self.get_instance_error
        return next(item for item in self.instances if item.id == instance_id)

    def list_instances(
        self, owner_user_id: int, limit: int, before_id: int | None
    ) -> tuple[AgentInstance, ...]:
        self.calls.append(("list_instances", (owner_user_id, limit, before_id)))
        return self.instances

    def get_run(self, owner_user_id: int, run_id: int) -> Run:
        self.calls.append(("get_run", (owner_user_id, run_id)))
        if self.get_run_error is not None:
            raise self.get_run_error
        return next(item for item in self.runs if item.id == run_id)

    def list_runs(
        self, owner_user_id: int, instance_id: int, limit: int, before_id: int | None
    ) -> tuple[Run, ...]:
        self.calls.append(("list_runs", (owner_user_id, instance_id, limit, before_id)))
        return self.runs

    def list_run_events(self, run_id: int, after_sequence: int, limit: int) -> tuple[RunEvent, ...]:
        self.calls.append(("list_run_events", (run_id, after_sequence, limit)))
        return self.events

    def create_instance(
        self,
        owner_user_id: int,
        definition_id: AgentDefinitionId,
        configuration: InstanceConfiguration,
        enabled: bool,
        now: datetime,
    ) -> AgentInstance:
        raise AssertionError("a read pass-through must never create an instance")

    def update_instance(
        self,
        owner_user_id: int,
        instance_id: int,
        configuration: InstanceConfiguration,
        now: datetime,
    ) -> AgentInstance:
        raise AssertionError("a read pass-through must never update an instance")

    def set_instance_enabled(
        self, owner_user_id: int, instance_id: int, enabled: bool, now: datetime
    ) -> AgentInstance:
        raise AssertionError("a read pass-through must never toggle an instance")


def service(persistence: RecordingPersistence) -> AgentService:
    return AgentService(persistence, create_builtin_definition_registry(), lambda: NOW)


def test_get_instance_delegates_with_owner_scope() -> None:
    persistence = RecordingPersistence(instances=(instance(7, 3),))

    result = service(persistence).get_instance(3, 7)

    assert persistence.calls == [("get_instance", (3, 7))]
    assert result.id == 7
    assert result.owner_user_id == 3


def test_get_instance_propagates_not_found_without_extra_reads() -> None:
    persistence = RecordingPersistence(instances=(instance(),))
    persistence.get_instance_error = AgentInstanceNotFound

    with pytest.raises(AgentInstanceNotFound):
        service(persistence).get_instance(2, 1)

    assert persistence.calls == [("get_instance", (2, 1))]


def test_list_instances_passes_pagination_through_unchanged() -> None:
    persistence = RecordingPersistence(instances=(instance(2), instance(1)))

    result = service(persistence).list_instances(5, 37, 91)

    assert persistence.calls == [("list_instances", (5, 37, 91))]
    assert [item.id for item in result] == [2, 1]


def test_list_instances_keeps_a_null_cursor_null() -> None:
    persistence = RecordingPersistence()

    service(persistence).list_instances(1, 20, None)

    assert persistence.calls == [("list_instances", (1, 20, None))]


def test_get_run_delegates_with_owner_scope() -> None:
    persistence = RecordingPersistence(runs=(run(9, 4),))

    result = service(persistence).get_run(4, 9)

    assert persistence.calls == [("get_run", (4, 9))]
    assert result.id == 9


def test_get_run_propagates_not_found_without_extra_reads() -> None:
    persistence = RecordingPersistence(runs=(run(),))
    persistence.get_run_error = RunNotFound

    with pytest.raises(RunNotFound):
        service(persistence).get_run(2, 1)

    assert persistence.calls == [("get_run", (2, 1))]


def test_list_runs_resolves_the_owned_parent_before_listing() -> None:
    """The parent is resolved first, so "no runs yet" stays distinct from "not your instance"."""
    persistence = RecordingPersistence(instances=(instance(8),), runs=(run(3, 8),))

    result = service(persistence).list_runs(6, 8, 50, 100)

    assert persistence.calls == [("get_instance", (6, 8)), ("list_runs", (6, 8, 50, 100))]
    assert [item.id for item in result] == [3]


def test_list_runs_keeps_a_null_cursor_null() -> None:
    persistence = RecordingPersistence(instances=(instance(2),))

    service(persistence).list_runs(1, 2, 20, None)

    assert persistence.calls == [("get_instance", (1, 2)), ("list_runs", (1, 2, 20, None))]


def test_list_runs_stops_when_the_owned_parent_is_not_found() -> None:
    """A foreign or nonexistent instance must fail before any Run row is read at all."""
    persistence = RecordingPersistence(runs=(run(),))
    persistence.get_instance_error = AgentInstanceNotFound

    with pytest.raises(AgentInstanceNotFound):
        service(persistence).list_runs(2, 7, 20, None)

    assert persistence.calls == [("get_instance", (2, 7))]


def test_reads_apply_no_default_limit_or_reordering() -> None:
    """The application layer must not invent pagination policy; persistence owns it."""
    persistence = RecordingPersistence(instances=(instance(1), instance(2)))

    # Deliberately passed in ascending id order: the service must not sort or slice.
    result = service(persistence).list_instances(1, 50, None)

    assert [item.id for item in result] == [1, 2]
    assert persistence.calls == [("list_instances", (1, 50, None))]


def test_run_usage_defaults_are_not_derived_by_reads() -> None:
    persistence = RecordingPersistence(runs=(run(),))

    result = service(persistence).get_run(1, 1)

    assert result.usage == ModelUsage(None, None, None)
    assert result.usage.total_tokens is None


# ---------------------------------------------------------------------------------------
# C7 Event reads: ownership is settled first, and the bound is refused before any query
# ---------------------------------------------------------------------------------------


def event(sequence: int = 1) -> RunEvent:
    return RunEvent(
        1,
        1,
        1,
        None,
        sequence,
        RunEventType.RUN_CREATED,
        None,
        None,
        None,
        None,
        NOW,
    )


def test_the_event_read_resolves_ownership_before_it_reads_anything() -> None:
    """The Run's own ownership rule settles this, so a foreign Run fails identically to a missing
    one and no Event row is read at all."""
    persistence = RecordingPersistence(runs=(run(),), events=(event(),))
    persistence.get_run_error = RunNotFound

    with pytest.raises(RunNotFound):
        service(persistence).list_run_events(2, 7, 0, 50)

    assert persistence.calls == [("get_run", (2, 7))]


def test_the_event_read_passes_the_cursor_and_bound_through_unchanged() -> None:
    """No default limit and no reordering is invented here: persistence owns both."""
    persistence = RecordingPersistence(runs=(run(),), events=(event(4), event(5)))

    result = service(persistence).list_run_events(1, 1, 3, 200)

    assert [item.sequence for item in result] == [4, 5]
    assert persistence.calls == [("get_run", (1, 1)), ("list_run_events", (1, 3, 200))]


@pytest.mark.parametrize("limit", [0, -1, EVENT_PAGE_LIMIT_MAX + 1])
def test_an_unusable_event_page_size_is_refused_before_any_query(limit: int) -> None:
    persistence = RecordingPersistence(runs=(run(),))

    with pytest.raises(ValueError, match="limit"):
        service(persistence).list_run_events(1, 1, 0, limit)

    assert persistence.calls == []


def test_a_negative_cursor_is_refused_before_any_query() -> None:
    persistence = RecordingPersistence(runs=(run(),))

    with pytest.raises(ValueError, match="after_sequence"):
        service(persistence).list_run_events(1, 1, -1, 50)

    assert persistence.calls == []
