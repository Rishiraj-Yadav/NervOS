"""Account dispatch authority reuses live Stage-D grants and exact Attempt fencing."""

from datetime import datetime

from sqlalchemy import Engine, select

from nervos_core.application.account_connections import ConnectionUnavailableError
from nervos_core.application.tool_invocations import ClaimHandle
from nervos_core.domain.runs import Run
from nervos_core.domain.tools import ToolDescriptor
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    JobAttemptRecord,
    JobRecord,
    RunRecord,
    ToolDefinitionRecord,
)
from nervos_core.infrastructure.database.tools import evaluate_permission_on_connection


class SqlAlchemyAccountDispatchAuthority:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def authorize(
        self, *, run: Run, claim: ClaimHandle, descriptor: ToolDescriptor, now: datetime
    ) -> int:
        with self._engine.connect() as conn:
            reviewed = conn.scalar(
                select(ToolDefinitionRecord.id).where(
                    ToolDefinitionRecord.id == descriptor.tool_definition_id,
                    ToolDefinitionRecord.fingerprint == descriptor.fingerprint,
                    ToolDefinitionRecord.upstream_name == descriptor.upstream_name,
                    ToolDefinitionRecord.source_kind == descriptor.source_kind.value,
                    ToolDefinitionRecord.source_id == descriptor.source_id,
                    ToolDefinitionRecord.status == "available",
                )
            )
            if reviewed is None:
                raise ConnectionUnavailableError
            owner = conn.scalar(
                select(AgentInstanceRecord.owner_user_id)
                .join(RunRecord, RunRecord.agent_instance_id == AgentInstanceRecord.id)
                .join(JobRecord, JobRecord.run_id == RunRecord.id)
                .join(JobAttemptRecord, JobAttemptRecord.job_id == JobRecord.id)
                .where(
                    RunRecord.id == run.id,
                    RunRecord.id == claim.run_id,
                    AgentInstanceRecord.id == run.agent_instance_id,
                    JobRecord.id == claim.job_id,
                    JobRecord.status == "running",
                    JobRecord.claim_token == claim.claim_token,
                    JobRecord.claimed_by == claim.worker_id,
                    JobRecord.cancel_requested_at.is_(None),
                    JobAttemptRecord.id == claim.attempt_id,
                    JobAttemptRecord.status == "running",
                    JobAttemptRecord.worker_id == claim.worker_id,
                    JobAttemptRecord.claim_token == claim.claim_token,
                    JobAttemptRecord.execution_started_at.is_not(None),
                    JobAttemptRecord.lease_expires_at > now,
                )
            )
            if (
                owner is None
                or not evaluate_permission_on_connection(
                    conn, run_id=run.id, tool_definition_id=descriptor.tool_definition_id
                ).allowed
            ):
                raise ConnectionUnavailableError
            return int(owner)
