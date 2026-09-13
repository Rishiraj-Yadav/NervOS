"""SQLAlchemy persistence for Agent Instances and immutable-snapshot Runs."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from sqlalchemy import insert, literal, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from nervos_core.application.agents import (
    AgentInstanceNotFound,
    AgentInstanceUnavailable,
    InstanceConfiguration,
    RunNotFound,
    RunTransitionRejected,
)
from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.domain.agents import AgentDefinitionId, AgentInstance
from nervos_core.domain.runs import ModelUsage, Run, RunLimits, RunStatus
from nervos_core.infrastructure.database.models import AgentInstanceRecord, RunRecord


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

    def create_run_for_owned_instance(
        self,
        owner_user_id: int,
        instance_id: int,
        definition_id: AgentDefinitionId,
        input_text: str,
        limits: RunLimits,
        now: datetime,
    ) -> Run:
        source = select(
            AgentInstanceRecord.id,
            literal("created"),
            AgentInstanceRecord.agent_key,
            AgentInstanceRecord.agent_definition_version,
            AgentInstanceRecord.model_provider,
            AgentInstanceRecord.model_name,
            literal(input_text),
            *(literal(value) for value in limits.values()),
            literal(now),
        ).where(
            AgentInstanceRecord.id == instance_id,
            AgentInstanceRecord.owner_user_id == owner_user_id,
            AgentInstanceRecord.enabled.is_(True),
            AgentInstanceRecord.agent_key == definition_id.agent_key,
            AgentInstanceRecord.agent_definition_version == definition_id.agent_definition_version,
        )
        columns = [
            "agent_instance_id",
            "status",
            "agent_key",
            "agent_definition_version",
            "model_provider",
            "model_name",
            "input_text",
            "input_max_bytes",
            "input_max_code_points",
            "output_max_bytes",
            "output_max_code_points",
            "provider_timeout_ms",
            "max_output_tokens",
            "max_model_calls",
            "created_at",
        ]
        try:
            with self._sessions.begin() as session:
                result = session.execute(
                    insert(RunRecord).from_select(columns, source).returning(RunRecord.id)
                )
                run_id = result.scalar_one_or_none()
                if run_id is None:
                    raise AgentInstanceUnavailable
                record = session.get(RunRecord, run_id)
                assert record is not None
                run = self._run(record)
            return run
        except SQLAlchemyError as error:
            raise PersistenceUnavailable from error

    def get_run(self, owner_user_id: int, run_id: int) -> Run:
        try:
            with self._sessions() as session:
                record = session.scalar(
                    select(RunRecord)
                    .join(
                        AgentInstanceRecord, AgentInstanceRecord.id == RunRecord.agent_instance_id
                    )
                    .where(
                        RunRecord.id == run_id, AgentInstanceRecord.owner_user_id == owner_user_id
                    )
                )
                if record is None:
                    raise RunNotFound
                return self._run(record)
        except SQLAlchemyError as error:
            raise PersistenceUnavailable from error

    def list_runs(
        self, owner_user_id: int, instance_id: int, limit: int, before_id: int | None
    ) -> tuple[Run, ...]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        query = (
            select(RunRecord)
            .join(AgentInstanceRecord)
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
                    self._run(record)
                    for record in session.scalars(query.order_by(RunRecord.id.desc()).limit(limit))
                )
        except SQLAlchemyError as error:
            raise PersistenceUnavailable from error

    def mark_running(self, owner_user_id: int, run_id: int, now: datetime) -> Run:
        return self._transition(
            owner_user_id,
            run_id,
            RunStatus.CREATED,
            {"status": RunStatus.RUNNING, "started_at": now},
        )

    def mark_succeeded(
        self,
        owner_user_id: int,
        run_id: int,
        output_text: str,
        finish_reason: str | None,
        usage: ModelUsage,
        elapsed_ms: int,
        now: datetime,
    ) -> Run:
        return self._transition(
            owner_user_id,
            run_id,
            RunStatus.RUNNING,
            {
                "status": RunStatus.SUCCEEDED,
                "finished_at": now,
                "output_text": output_text,
                "finish_reason": finish_reason,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
                "elapsed_ms": elapsed_ms,
            },
        )

    def mark_failed(
        self,
        owner_user_id: int,
        run_id: int,
        error_code: str,
        error_message: str,
        usage: ModelUsage,
        elapsed_ms: int,
        now: datetime,
    ) -> Run:
        return self._transition(
            owner_user_id,
            run_id,
            RunStatus.RUNNING,
            {
                "status": RunStatus.FAILED,
                "finished_at": now,
                "error_code": error_code,
                "error_message": error_message,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
                "elapsed_ms": elapsed_ms,
            },
        )

    def _transition(
        self, owner_user_id: int, run_id: int, expected: RunStatus, values: dict[str, object]
    ) -> Run:
        owned = (
            select(AgentInstanceRecord.id)
            .where(
                AgentInstanceRecord.id == RunRecord.agent_instance_id,
                AgentInstanceRecord.owner_user_id == owner_user_id,
            )
            .exists()
        )
        try:
            with self._sessions.begin() as session:
                result = session.execute(
                    update(RunRecord)
                    .where(RunRecord.id == run_id, RunRecord.status == expected, owned)
                    .values(**values)
                )
                if cast(CursorResult[Any], result).rowcount != 1:
                    raise RunTransitionRejected
                record = session.get(RunRecord, run_id)
                assert record is not None
                run = self._run(record)
            return run
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
    def _run(record: RunRecord) -> Run:
        limits = RunLimits(
            record.input_max_bytes,
            record.input_max_code_points,
            record.output_max_bytes,
            record.output_max_code_points,
            record.provider_timeout_ms,
            record.max_output_tokens,
            record.max_model_calls,
        )
        usage = ModelUsage(record.input_tokens, record.output_tokens, record.total_tokens)
        return Run(
            record.id,
            record.agent_instance_id,
            record.agent_key,
            record.agent_definition_version,
            record.model_provider,
            record.model_name,
            record.input_text,
            limits,
            RunStatus(record.status),
            record.created_at,
            record.started_at,
            record.finished_at,
            record.output_text,
            record.finish_reason,
            record.error_code,
            record.error_message,
            usage,
            record.elapsed_ms,
        )
