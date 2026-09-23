"""Scheduler composition root.

This module wires exactly four things together: an engine, the trigger persistence that reads and
writes durable schedule state, the schedule evaluator, and the service that decides what to do
about each due schedule. It deliberately imports no model provider, no MCP package, no tool
registry and no execution service, because a scheduler that could reach any of those is a
scheduler whose job has quietly stopped being "decide when".

The schema expectation is declared **here**, locally, rather than imported from the Worker. Each
process states the revision it was built for; sharing one constant would mean a Worker released
ahead of the Scheduler could silently change what the Scheduler accepts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nervos_core.application.agent_definitions import create_builtin_definition_registry
from nervos_core.application.scheduler import SchedulerService
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.triggers import SqlAlchemyTriggerPersistence
from nervos_core.infrastructure.scheduling import create_schedule_evaluator
from sqlalchemy import Engine

from nervos_scheduler.config import SchedulerSettings

#: The one migration revision this process understands. F3 ships migration 0011.
EXPECTED_SCHEMA_REVISION = "0011_stage_f3_scoped_memory"


def utc_now() -> datetime:
    """The one clock read in this process. Everything downstream receives an instant."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SchedulerComposition:
    """One fully constructed scheduler process, and the resources it owns."""

    engine: Engine
    service: SchedulerService
    settings: SchedulerSettings


def create_scheduler(settings: SchedulerSettings) -> SchedulerComposition:
    """Compose a Scheduler from validated settings.

    Admission capacity and retry budgets are deliberately not configurable here: they belong to the
    submission primitive this process calls, and a scheduler that could set its own would be able
    to create Runs the control plane would refuse.
    """
    engine = create_sqlite_engine(settings.database_path)
    persistence = SqlAlchemyTriggerPersistence(engine)
    service = SchedulerService(
        create_builtin_definition_registry(),
        persistence,
        create_schedule_evaluator(),
    )
    return SchedulerComposition(engine=engine, service=service, settings=settings)


def close_scheduler(composition: SchedulerComposition) -> None:
    """Release everything the composition owns.

    There is nothing to drain and nothing to deregister. The Scheduler holds no lease, no
    registration, no remote session and no in-flight claim, so a shutdown cannot strand work: a Run
    it created is already committed and is owned by C3 from that moment, and a schedule it did not
    reach is still due on the next start.
    """
    composition.engine.dispose()


def write_ready_marker(path: Path, *, revision: str) -> None:
    """Write the test-only readiness marker.

    Production never sets `NERVOS_SCHEDULER_READY_FILE`, so production never writes a file. The
    marker records the schema revision and nothing else — this process has no credential and no
    provider identity to record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"schema_revision={revision}\n", encoding="utf-8")


__all__ = [
    "EXPECTED_SCHEMA_REVISION",
    "SchedulerComposition",
    "close_scheduler",
    "create_scheduler",
    "utc_now",
    "write_ready_marker",
]
