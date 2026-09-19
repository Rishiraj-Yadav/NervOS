"""Shared harness for the E2 scheduler suites.

Everything here is synchronous on purpose. The scheduler has no network, no provider and no tool
execution, so there is nothing to keep off the event loop and no concurrency to await: a tick is a
bounded local read followed by short local transactions.

The clock is never real. A schedule is made due by *storing* an instant in the past and then asking
for a tick at a named `now`, so no test in this suite waits for time to pass.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from nervos_core.application.agent_definitions import create_builtin_definition_registry
from nervos_core.application.scheduler import SchedulerService
from nervos_core.application.triggers import TriggerDraft
from nervos_core.domain.triggers import (
    OccurrenceStatus,
    ScheduleSpec,
    TriggerDefinition,
    TriggerKind,
)
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.models import TriggerDefinitionRecord
from nervos_core.infrastructure.database.triggers import SqlAlchemyTriggerPersistence
from nervos_core.infrastructure.scheduling import create_schedule_evaluator
from sqlalchemy import Engine, select, text

ROOT = Path(__file__).resolve().parents[4]
OWNER = 1
OTHER_OWNER = 2
AGENT = 1
#: The instant every relative schedule in this suite is expressed against.
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class StoredSchedule:
    """The two durable facts a scheduler decision depends on, read straight from the row."""

    enabled: bool
    next_fire_at: datetime | None
    config_revision: int


@dataclass(slots=True)
class Rig:
    """One migrated database and the composition that schedules against it."""

    engine: Engine
    triggers: SqlAlchemyTriggerPersistence
    service: SchedulerService
    path: Path

    def create(
        self,
        *,
        kind: TriggerKind = TriggerKind.INTERVAL,
        next_fire_at: datetime | None = None,
        schedule: ScheduleSpec | None = None,
        input_text: str = "scheduled work",
        agent_instance_id: int = AGENT,
        owner: int = OWNER,
        enabled: bool = True,
        display_name: str = "Scheduled",
    ) -> TriggerDefinition:
        """Create one trigger, defaulting to a five-minute interval due at `NOW`."""
        moment = NOW if next_fire_at is None else next_fire_at
        spec = schedule if schedule is not None else _default_spec(kind, moment)
        stored_fire = None if not enabled else moment
        return self.triggers.create_trigger(
            owner,
            TriggerDraft(
                agent_instance_id=agent_instance_id,
                display_name=display_name,
                input_text=input_text,
                kind=kind,
                schedule=spec,
                enabled=enabled,
                next_fire_at=stored_fire,
            ),
            NOW,
        )

    def schedule(self, trigger_id: int) -> StoredSchedule:
        """Read the durable facts straight off the row, through the typed ORM columns.

        Raw SQL would hand back the stored text for a `DateTime` column, which is a different shape
        from what the scheduler itself sees; going through the model keeps this harness reading the
        same values the production path does.
        """
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(
                        TriggerDefinitionRecord.enabled,
                        TriggerDefinitionRecord.next_fire_at,
                        TriggerDefinitionRecord.config_revision,
                    ).where(TriggerDefinitionRecord.id == trigger_id)
                )
                .mappings()
                .one()
            )
        return StoredSchedule(
            enabled=bool(row["enabled"]),
            next_fire_at=_utc(row["next_fire_at"]),
            config_revision=int(row["config_revision"]),
        )

    def occurrences(self, trigger_id: int) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    text(
                        "SELECT id, status, skip_code, run_id, nominal_at, trigger_revision "
                        "FROM trigger_occurrences WHERE trigger_definition_id = :trigger "
                        "ORDER BY id"
                    ),
                    {"trigger": trigger_id},
                )
                .mappings()
                .all()
            ]

    def counts(self) -> dict[str, int]:
        """Durable row counts for the tables a materialization may touch."""
        fields = {
            "runs": "runs",
            "jobs": "jobs",
            "attempts": "job_attempts",
            "occurrences": "trigger_occurrences",
        }
        with self.engine.connect() as connection:
            return {
                name: int(connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
                for name, table in fields.items()
            }

    def run_ids(self) -> list[int]:
        with self.engine.connect() as connection:
            return [
                int(value)
                for value in connection.scalars(text("SELECT id FROM runs ORDER BY id")).all()
            ]

    def set_agent_enabled(self, enabled: bool) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE agent_instances SET enabled = :enabled WHERE id = :agent"),
                {"enabled": 1 if enabled else 0, "agent": AGENT},
            )

    def set_agent_definition(self, version: str) -> None:
        """Move the Agent Instance onto a different Agent Definition."""
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE agent_instances SET agent_definition_version = :version "
                    "WHERE id = :agent"
                ),
                {"version": version, "agent": AGENT},
            )

    def tick(self, now: datetime = NOW) -> Any:
        return self.service.tick(now)


def _default_spec(kind: TriggerKind, moment: datetime) -> ScheduleSpec:
    if kind is TriggerKind.ONE_TIME:
        return ScheduleSpec.one_time(moment)
    if kind is TriggerKind.INTERVAL:
        return ScheduleSpec.interval(300)
    if kind is TriggerKind.CRON:
        return ScheduleSpec.cron("0 9 * * *", "UTC")
    # Webhook and event drafts carry their own identity fields, which the scheduler never sees.
    raise AssertionError("the scheduler harness only builds schedule kinds")


def _utc(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise AssertionError("a stored instant was not a timestamp")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def migrate(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agents: int = 1,
    max_pending: int = 1000,
    sleep: bool = False,
) -> Engine:
    """Create one disposable migrated database holding two owners and `agents` Agent Instances.

    The Agent Instances carry the real built-in definition identity, so a Run created here goes
    through exactly the submission a manual Run does, including the Agent/definition consistency
    predicate the canonical helper enforces.
    """
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(path))
    command.upgrade(Config(str(ROOT / "apps" / "api" / "alembic.ini")), "head")
    engine = create_sqlite_engine(path)
    with engine.begin() as connection:
        for owner in (OWNER, OTHER_OWNER):
            connection.execute(
                text(
                    "INSERT INTO users(username,password_hash,role,is_active,created_at,updated_at)"
                    " VALUES(:username,X'00','admin',1,:now,:now)"
                ),
                {"username": f"owner{owner}", "now": NOW},
            )
        for index in range(1, agents + 1):
            connection.execute(
                text(
                    "INSERT INTO agent_instances(owner_user_id,agent_key,"
                    "agent_definition_version,display_name,enabled,model_provider,model_name,"
                    "created_at,updated_at) "
                    "VALUES(1,'nervos.chat','2',:name,1,'anthropic','opaque/model',:now,:now)"
                ),
                {"name": f"Agent {index}", "now": NOW},
            )
    return engine


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    """A migrated database with a real scheduler composition over it.

    `sleep=lambda _: None` removes the shared transaction runner's contention backoff, so a test
    that deliberately contends does not spend real seconds proving it.
    """
    path = tmp_path / "nervos.db"
    engine = migrate(path, monkeypatch)
    triggers = SqlAlchemyTriggerPersistence(engine, sleep=lambda _: None)
    service = SchedulerService(
        create_builtin_definition_registry(), triggers, create_schedule_evaluator()
    )
    try:
        yield Rig(engine=engine, triggers=triggers, service=service, path=path)
    finally:
        engine.dispose()


def interval_trigger(
    rig: Rig, *, seconds: int = 300, due_at: datetime | None = None
) -> TriggerDefinition:
    """A five-minute interval trigger due at `due_at` (default: one period before `NOW`)."""
    moment = (NOW - timedelta(seconds=seconds)) if due_at is None else due_at
    return rig.create(
        kind=TriggerKind.INTERVAL,
        next_fire_at=moment,
        schedule=ScheduleSpec.interval(seconds),
    )


def cron_trigger(
    rig: Rig, *, expression: str = "0 9 * * *", timezone: str = "UTC", due_at: datetime
) -> TriggerDefinition:
    return rig.create(
        kind=TriggerKind.CRON,
        next_fire_at=due_at,
        schedule=ScheduleSpec.cron(expression, timezone),
    )


def occurrence_statuses(rig: Rig, trigger_id: int) -> list[OccurrenceStatus]:
    return [OccurrenceStatus(row["status"]) for row in rig.occurrences(trigger_id)]


__all__ = [
    "AGENT",
    "NOW",
    "OTHER_OWNER",
    "OWNER",
    "Rig",
    "StoredSchedule",
    "cron_trigger",
    "interval_trigger",
    "migrate",
    "occurrence_statuses",
    "replace",
    "rig",
]
