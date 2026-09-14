"""Dormant C1 durable submission and Run-event persistence."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Engine, func, insert, select
from sqlalchemy.exc import SQLAlchemyError

from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.jobs import RunEventType
from nervos_core.domain.runs import RunLimits, validate_input_text
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    JobRecord,
    RunEventRecord,
    RunRecord,
)


class DurableSubmissionRejected(ValueError):
    """The owned enabled Agent Instance was unavailable at commit time."""


class SqlAlchemyJobPersistence:
    """Connection-transaction persistence, intentionally unwired from production in C1."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def submit(
        self,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        input_text: str,
        limits: RunLimits,
        definition_id: AgentDefinitionId | None = None,
        now: datetime,
        max_attempts: int = 3,
    ) -> tuple[int, int]:
        """Atomically snapshot an owned enabled Instance into Run, Job, and two events."""
        validate_input_text(input_text, limits)
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        connection = self._engine.connect()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            predicates = [
                AgentInstanceRecord.id == agent_instance_id,
                AgentInstanceRecord.owner_user_id == owner_user_id,
                AgentInstanceRecord.enabled.is_(True),
            ]
            if definition_id is not None:
                predicates.extend(
                    [
                        AgentInstanceRecord.agent_key == definition_id.agent_key,
                        AgentInstanceRecord.agent_definition_version
                        == definition_id.agent_definition_version,
                    ]
                )
            instance = (
                connection.execute(select(AgentInstanceRecord).where(*predicates))
                .mappings()
                .one_or_none()
            )
            if instance is None:
                raise DurableSubmissionRejected
            run_result = connection.execute(
                insert(RunRecord).values(
                    agent_instance_id=agent_instance_id,
                    status="created",
                    agent_key=instance["agent_key"],
                    agent_definition_version=instance["agent_definition_version"],
                    model_provider=instance["model_provider"],
                    model_name=instance["model_name"],
                    input_text=input_text,
                    input_max_bytes=limits.input_max_bytes,
                    input_max_code_points=limits.input_max_code_points,
                    output_max_bytes=limits.output_max_bytes,
                    output_max_code_points=limits.output_max_code_points,
                    provider_timeout_ms=limits.provider_timeout_ms,
                    max_output_tokens=limits.max_output_tokens,
                    max_model_calls=limits.max_model_calls,
                    created_at=now,
                )
            )
            run_pk = run_result.inserted_primary_key
            if run_pk is None or run_pk[0] is None:
                raise PersistenceUnavailable
            run_id = int(run_pk[0])
            job_result = connection.execute(
                insert(JobRecord).values(
                    run_id=run_id,
                    agent_instance_id=agent_instance_id,
                    model_provider=instance["model_provider"],
                    status="queued",
                    available_at=now,
                    attempt_count=0,
                    max_attempts=max_attempts,
                    created_at=now,
                    updated_at=now,
                )
            )
            job_pk = job_result.inserted_primary_key
            if job_pk is None or job_pk[0] is None:
                raise PersistenceUnavailable
            job_id = int(job_pk[0])
            connection.execute(
                insert(RunEventRecord),
                [
                    {
                        "run_id": run_id,
                        "job_id": job_id,
                        "sequence": 1,
                        "event_type": "run.created",
                        "created_at": now,
                    },
                    {
                        "run_id": run_id,
                        "job_id": job_id,
                        "sequence": 2,
                        "event_type": "run.queued",
                        "available_at": now,
                        "created_at": now,
                    },
                ],
            )
            connection.commit()
            return run_id, job_id
        except DurableSubmissionRejected:
            connection.rollback()
            raise
        except SQLAlchemyError as error:
            connection.rollback()
            raise PersistenceUnavailable from error
        finally:
            connection.close()

    def append_event(
        self,
        *,
        run_id: int,
        job_id: int,
        event_type: RunEventType | str,
        created_at: datetime,
        attempt_id: int | None = None,
        code: str | None = None,
        message: str | None = None,
        attempt_number: int | None = None,
        available_at: datetime | None = None,
    ) -> int:
        """Append one typed event using per-Run serialization via BEGIN IMMEDIATE."""
        connection = self._engine.connect()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            sequence = int(
                connection.scalar(
                    select(func.coalesce(func.max(RunEventRecord.sequence), 0) + 1).where(
                        RunEventRecord.run_id == run_id
                    )
                )
                or 1
            )
            connection.execute(
                insert(RunEventRecord).values(
                    run_id=run_id,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    sequence=sequence,
                    event_type=event_type,
                    code=code,
                    message=message,
                    attempt_number=attempt_number,
                    available_at=available_at,
                    created_at=created_at,
                )
            )
            connection.commit()
            return sequence
        except SQLAlchemyError as error:
            connection.rollback()
            raise PersistenceUnavailable from error
        finally:
            connection.close()
