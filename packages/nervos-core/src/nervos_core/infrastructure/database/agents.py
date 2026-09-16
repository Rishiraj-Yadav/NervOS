"""SQLAlchemy persistence for Agent Instances and immutable-snapshot Runs."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from nervos_core.application.agents import (
    EVENT_PAGE_LIMIT_MAX,
    AgentInstanceNotFound,
    InstanceConfiguration,
    RunNotFound,
)
from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.domain.agents import AgentDefinitionId, AgentInstance
from nervos_core.domain.jobs import JobStatus, RunEvent
from nervos_core.domain.runs import Run
from nervos_core.infrastructure.database.jobs import run_event_from_record, run_from_record
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    JobRecord,
    RunEventRecord,
    RunRecord,
)


def _phase_of(job: JobRecord | None) -> tuple[JobStatus | None, datetime | None]:
    """Derive the read-only execution phase, and a retry instant only while one is pending.

    The phase is the Job's own durable status verbatim: no second vocabulary is invented, and
    nothing here is stored, so it cannot drift from the state it describes. A retry instant is
    surfaced only in the one phase where it means something.
    """
    if job is None:
        return None, None
    phase = JobStatus(job.status)
    return phase, job.available_at if phase is JobStatus.RETRY_WAIT else None


class SqlAlchemyAgentPersistence:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def create_instance(
        self,
        owner_user_id: int,
        definition_id: AgentDefinitionId,
        configuration: InstanceConfiguration,
        enabled: bool,
        now: datetime,
    ) -> AgentInstance:
        try:
            with self._sessions.begin() as session:
                record = AgentInstanceRecord(
                    owner_user_id=owner_user_id,
                    agent_key=definition_id.agent_key,
                    agent_definition_version=definition_id.agent_definition_version,
                    display_name=configuration.display_name,
                    enabled=enabled,
                    model_provider=configuration.model_provider,
                    model_name=configuration.model_name,
                    created_at=now,
                    updated_at=now,
                )
                session.add(record)
                session.flush()
                result = self._instance(record)
            return result
        except SQLAlchemyError as error:
            raise PersistenceUnavailable from error

    def get_instance(self, owner_user_id: int, instance_id: int) -> AgentInstance:
        try:
            with self._sessions() as session:
                record = session.scalar(
                    select(AgentInstanceRecord).where(
                        AgentInstanceRecord.id == instance_id,
                        AgentInstanceRecord.owner_user_id == owner_user_id,
                    )
                )
                if record is None:
                    raise AgentInstanceNotFound
                return self._instance(record)
        except SQLAlchemyError as error:
            raise PersistenceUnavailable from error

    def list_instances(
        self, owner_user_id: int, limit: int, before_id: int | None
    ) -> tuple[AgentInstance, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        query = select(AgentInstanceRecord).where(
            AgentInstanceRecord.owner_user_id == owner_user_id
        )
        if before_id is not None:
            query = query.where(AgentInstanceRecord.id < before_id)
        try:
            with self._sessions() as session:
                return tuple(
                    self._instance(record)
                    for record in session.scalars(
                        query.order_by(AgentInstanceRecord.id.desc()).limit(limit)
                    )
                )
        except SQLAlchemyError as error:
            raise PersistenceUnavailable from error

    def update_instance(
        self,
        owner_user_id: int,
        instance_id: int,
        configuration: InstanceConfiguration,
        now: datetime,
    ) -> AgentInstance:
        values = {
            "display_name": configuration.display_name,
            "model_provider": configuration.model_provider,
            "model_name": configuration.model_name,
            "updated_at": now,
        }
        return self._update_instance(owner_user_id, instance_id, values)

    def set_instance_enabled(
        self, owner_user_id: int, instance_id: int, enabled: bool, now: datetime
    ) -> AgentInstance:
        return self._update_instance(
            owner_user_id, instance_id, {"enabled": enabled, "updated_at": now}
        )

    def _update_instance(
        self, owner_user_id: int, instance_id: int, values: Mapping[str, object]
    ) -> AgentInstance:
        try:
            with self._sessions.begin() as session:
                result = session.execute(
                    update(AgentInstanceRecord)
                    .where(
                        AgentInstanceRecord.id == instance_id,
                        AgentInstanceRecord.owner_user_id == owner_user_id,
                    )
                    .values(**values)
                )
                if cast(CursorResult[Any], result).rowcount != 1:
                    raise AgentInstanceNotFound
                record = session.get(AgentInstanceRecord, instance_id)
                assert record is not None
                instance = self._instance(record)
            return instance
        except SQLAlchemyError as error:
            raise PersistenceUnavailable from error

    def get_run(self, owner_user_id: int, run_id: int) -> Run:
        try:
            with self._sessions() as session:
                row = session.execute(
                    select(RunRecord, JobRecord)
                    .select_from(RunRecord)
                    .join(
                        AgentInstanceRecord, AgentInstanceRecord.id == RunRecord.agent_instance_id
                    )
                    .outerjoin(JobRecord, JobRecord.run_id == RunRecord.id)
                    .where(
                        RunRecord.id == run_id, AgentInstanceRecord.owner_user_id == owner_user_id
                    )
                ).one_or_none()
                if row is None:
                    raise RunNotFound
                return self._run(row[0], row[1])
        except SQLAlchemyError as error:
            raise PersistenceUnavailable from error

    def list_runs(
        self, owner_user_id: int, instance_id: int, limit: int, before_id: int | None
    ) -> tuple[Run, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        query = (
            select(RunRecord, JobRecord)
            .select_from(RunRecord)
            .join(
                AgentInstanceRecord,
                AgentInstanceRecord.id == RunRecord.agent_instance_id,
            )
            .outerjoin(JobRecord, JobRecord.run_id == RunRecord.id)
            .where(
                RunRecord.agent_instance_id == instance_id,
                AgentInstanceRecord.owner_user_id == owner_user_id,
            )
        )
        if before_id is not None:
            query = query.where(RunRecord.id < before_id)
        try:
            with self._sessions() as session:
                return tuple(
                    self._run(record, job)
                    for record, job in session.execute(
                        query.order_by(RunRecord.id.desc()).limit(limit)
                    )
                )
        except SQLAlchemyError as error:
            raise PersistenceUnavailable from error

    def list_run_events(self, run_id: int, after_sequence: int, limit: int) -> tuple[RunEvent, ...]:
        """Return one ascending-sequence page of one Run's Events, strictly after a cursor.

        The predicate is the `UNIQUE(run_id, sequence)` index SQLite already maintains, so this is
        a bounded index range seek scoped to this one Run and this one sequence: the cost of a
        timeline grows with that Run's own history and never with the table.
        """
        if not 1 <= limit <= EVENT_PAGE_LIMIT_MAX:
            raise ValueError("limit must be between 1 and 200")
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        query = (
            select(RunEventRecord)
            .where(
                RunEventRecord.run_id == run_id,
                RunEventRecord.sequence > after_sequence,
            )
            .order_by(RunEventRecord.sequence.asc())
            .limit(limit)
        )
        try:
            with self._sessions() as session:
                return tuple(run_event_from_record(record) for record in session.scalars(query))
        except SQLAlchemyError as error:
            raise PersistenceUnavailable from error

    @staticmethod
    def _instance(record: AgentInstanceRecord) -> AgentInstance:
        return AgentInstance(
            record.id,
            record.owner_user_id,
            AgentDefinitionId(record.agent_key, record.agent_definition_version),
            record.display_name,
            record.enabled,
            record.model_provider,
            record.model_name,
            record.created_at,
            record.updated_at,
        )

    @staticmethod
    def _run(record: RunRecord, job: JobRecord | None) -> Run:
        phase, retry_available_at = _phase_of(job)
        return run_from_record(record, execution_phase=phase, retry_available_at=retry_available_at)
