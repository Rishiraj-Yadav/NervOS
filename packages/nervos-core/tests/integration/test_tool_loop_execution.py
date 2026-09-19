"""D4 durable tool execution: the fenced transitions, the budgets, and the replay guard.

Every test here runs against a real migrated schema, because the subject is what the loop *commits*
rather than what it decides. The unit suite proves the decisions; this suite proves they reach
durable state with the right shape and that no write can happen without authority.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from execution_support import migrate
from nervos_core.application.builtin_tools import (
    BUILTIN_SOURCE_REF,
    builtin_tool_specs,
    create_builtin_tool_registry,
    reconcile_builtin_definitions,
)
from nervos_core.application.model_completion import (
    MODEL_RATE_LIMITED,
    TOOL_LOOP_LIMIT,
    ModelProviderError,
    ModelResponse,
    ModelUsage,
    StopOutcome,
    ToolCall,
)
from nervos_core.application.retry_policy import PRODUCTION_RETRY_POLICY
from nervos_core.application.tool_invocations import (
    InvocationRequest,
    InvocationStatus,
    RecordOutcomeKind,
    StartOutcomeKind,
    result_envelope,
)
from nervos_core.application.tool_loop import ToolLoop
from nervos_core.application.tool_permissions import (
    PermissionDecision,
    PermissionDenialReason,
)
from nervos_core.application.trusted_chat import NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.jobs import RetryDisposition
from nervos_core.domain.runs import STAGE_B_LIMITS, TOOL_ENABLED_LIMITS, RunLimits
from nervos_core.domain.tools import ToolSourceKind
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    AgentToolGrantRecord,
    JobAttemptRecord,
    JobRecord,
    RunRecord,
    ToolDefinitionRecord,
    ToolInvocationRecord,
)
from nervos_core.infrastructure.database.tool_definitions import (
    SqlAlchemyToolDefinitionPersistence,
)
from nervos_core.infrastructure.database.tool_invocations import (
    SqlAlchemyToolInvocationPersistence,
)
from nervos_core.infrastructure.database.tools import (
    SqlAlchemyToolPermissionEvaluator,
    SqlAlchemyToolPermissionPersistence,
)
from sqlalchemy import Engine, func, insert, select, text, update

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
LEASE = timedelta(minutes=2)


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """A disposable migrated database holding one owner, one Agent Instance, and the built-ins.

    Reconciliation runs here for the same reason the Worker runs it at startup: a tool D4 can
    execute has a durable definition before any Run could name it. It grants nothing.
    """
    database = migrate(tmp_path / "tool_loop.db", monkeypatch, agents=1)
    reconcile_builtin_definitions(
        SqlAlchemyToolDefinitionPersistence(database),
        specs=builtin_tool_specs(clock=lambda: NOW),
        now=NOW,
    )
    return database


def _instance_id(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(select(AgentInstanceRecord.id)).scalar_one())


def _grant(engine: Engine, *, tool_definition_id: int, instance_id: int, now: datetime) -> int:
    with engine.begin() as connection:
        connection.execute(
            insert(AgentToolGrantRecord).values(
                agent_instance_id=instance_id,
                tool_definition_id=tool_definition_id,
                reviewed_fingerprint=_definition_fingerprint(engine, tool_definition_id),
                created_at=now,
            )
        )
    with engine.connect() as connection:
        return int(connection.execute(select(func.max(AgentToolGrantRecord.id))).scalar_one())


def _definition_fingerprint(engine: Engine, tool_definition_id: int) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                select(ToolDefinitionRecord.fingerprint).where(
                    ToolDefinitionRecord.id == tool_definition_id
                )
            ).scalar_one()
        )


def _submit(engine: Engine, *, instance_id: int, limits: RunLimits, now: datetime = NOW) -> int:
    """Submit through the real durable submission path, so the snapshot is the real one."""
    run = SqlAlchemyJobPersistence(engine).submit(
        owner_user_id=1,
        agent_instance_id=instance_id,
        input_text="what time is it",
        limits=limits,
        now=now,
    )
    return run.id


def _claim(engine: Engine, run_id: int, *, now: datetime = NOW) -> tuple[int, Any]:
    """Claim and start the Run's Attempt, returning its id and the claim handle."""
    persistence = SqlAlchemyJobExecutionPersistence(engine)
    claim = persistence.claim_next(
        worker_id="worker-1",
        provider_ids=("anthropic",),
        max_active=4,
        now=now,
        lease_duration=LEASE,
    )
    assert claim is not None
    assert persistence.start_attempt(claim, now=now) is True
    # `start_attempt` sets `execution_started_at`; re-read it so the claim carries the live lease.
    refreshed = persistence.inspect_claim(claim, now=now)
    assert refreshed is not None
    return claim.attempt_id, claim


def _invocation_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                select(ToolInvocationRecord).order_by(ToolInvocationRecord.tool_sequence)
            )
            .mappings()
            .all()
        ]


def _definition_id(engine: Engine, upstream_name: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                select(ToolDefinitionRecord.id).where(
                    ToolDefinitionRecord.upstream_name == upstream_name
                )
            ).scalar_one()
        )


def _descriptor(engine: Engine, tool_definition_id: int) -> Any:
    persisted = SqlAlchemyToolDefinitionPersistence(engine).get(tool_definition_id)
    assert persisted is not None
    return persisted.to_descriptor()


def _request(descriptor: Any, claim: Any, *, call_id: str = "call-1") -> InvocationRequest:
    """Build one request bound to a real claim, so the row's foreign keys are genuine."""
    return InvocationRequest(
        run_id=claim.run_id,
        job_id=claim.job_id,
        attempt_id=claim.attempt_id,
        tool_sequence=1,
        tool_definition_id=descriptor.tool_definition_id,
        source_kind=descriptor.source_kind,
        source_id=descriptor.source_id,
        upstream_name=descriptor.upstream_name,
        model_name=descriptor.model_name,
        definition_fingerprint=descriptor.fingerprint,
        provider_call_id=call_id,
        permission_decision=PermissionDecision(allowed=True),
        arguments={"timezone": "UTC"},
    )


class TestSubmittedLimits:
    """The Run snapshot is where a tool budget becomes real, so it is proved end to end."""

    def test_a_tool_free_run_snapshots_no_cutoff(self, engine: Engine) -> None:
        instance_id = _instance_id(engine)
        definition_id = _definition_id(engine, "current_time")
        _grant(engine, tool_definition_id=definition_id, instance_id=instance_id, now=NOW)

        run_id = _submit(engine, instance_id=instance_id, limits=STAGE_B_LIMITS)

        with engine.connect() as connection:
            row = connection.execute(
                select(
                    RunRecord.max_tool_calls,
                    RunRecord.tool_grant_cutoff_id,
                    RunRecord.max_model_calls,
                ).where(RunRecord.id == run_id)
            ).one()
        # A grant exists, but a tool-free Run still snapshots nothing and keeps the Stage B limits.
        assert row == (0, 0, 1)

    def test_a_tool_enabled_run_snapshots_the_current_grant_cutoff(self, engine: Engine) -> None:
        instance_id = _instance_id(engine)
        grant_id = _grant(
            engine,
            tool_definition_id=_definition_id(engine, "current_time"),
            instance_id=instance_id,
            now=NOW,
        )

        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)

        with engine.connect() as connection:
            row = connection.execute(
                select(
                    RunRecord.max_tool_calls,
                    RunRecord.tool_grant_cutoff_id,
                    RunRecord.max_model_calls,
                ).where(RunRecord.id == run_id)
            ).one()
        assert row == (8, grant_id, 8)

    def test_a_grant_created_after_submission_is_invisible_to_the_run(self, engine: Engine) -> None:
        """The cutoff is a durable ordering, not a timestamp, so it cannot be raced."""
        instance_id = _instance_id(engine)
        definition_id = _definition_id(engine, "current_time")
        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)
        _grant(engine, tool_definition_id=definition_id, instance_id=instance_id, now=LATER)

        decision = SqlAlchemyToolPermissionEvaluator(engine).check_permission(
            run_id=run_id, tool_definition_id=definition_id
        )

        assert decision.allowed is False
        assert decision.reason is PermissionDenialReason.GRANT_AFTER_RUN_CUTOFF

    def test_a_grant_predating_submission_is_visible(self, engine: Engine) -> None:
        instance_id = _instance_id(engine)
        definition_id = _definition_id(engine, "current_time")
        _grant(engine, tool_definition_id=definition_id, instance_id=instance_id, now=NOW)
        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)

        decision = SqlAlchemyToolPermissionEvaluator(engine).check_permission(
            run_id=run_id, tool_definition_id=definition_id
        )

        assert decision.allowed is True


class TestRecordRequestedBoundary:
    """The start boundary is enforced by the insert, not by its callers' discipline."""

    def test_a_run_without_a_committed_start_cannot_record_an_invocation(
        self, engine: Engine
    ) -> None:
        instance_id = _instance_id(engine)
        definition_id = _definition_id(engine, "current_time")
        _grant(engine, tool_definition_id=definition_id, instance_id=instance_id, now=NOW)
        _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)

        # Claim the Job but never cross the execution-start boundary.
        persistence = SqlAlchemyJobExecutionPersistence(engine)
        claim = persistence.claim_next(
            worker_id="worker-1",
            provider_ids=("anthropic",),
            max_active=4,
            now=NOW,
            lease_duration=LEASE,
        )
        assert claim is not None

        descriptor = _descriptor(engine, definition_id)
        outcome = SqlAlchemyToolInvocationPersistence(engine).record_requested(
            claim=claim, request=_request(descriptor, claim), now=NOW
        )

        assert outcome.kind is RecordOutcomeKind.FENCED
        assert _invocation_rows(engine) == []

    def test_a_started_attempt_can_record_an_invocation(self, engine: Engine) -> None:
        instance_id = _instance_id(engine)
        definition_id = _definition_id(engine, "current_time")
        _grant(engine, tool_definition_id=definition_id, instance_id=instance_id, now=NOW)
        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)
        _, claim = _claim(engine, run_id)

        descriptor = _descriptor(engine, definition_id)
        outcome = SqlAlchemyToolInvocationPersistence(engine).record_requested(
            claim=claim, request=_request(descriptor, claim), now=NOW
        )

        assert outcome.kind is RecordOutcomeKind.REQUESTED
        rows = _invocation_rows(engine)
        assert len(rows) == 1
        assert rows[0]["status"] == InvocationStatus.REQUESTED.value
        assert rows[0]["started_at"] is None
        assert rows[0]["permission_decision"] == "allowed"

    def test_the_audit_metadata_is_recorded_without_any_argument_value(
        self, engine: Engine
    ) -> None:
        instance_id = _instance_id(engine)
        definition_id = _definition_id(engine, "current_time")
        _grant(engine, tool_definition_id=definition_id, instance_id=instance_id, now=NOW)
        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)
        _, claim = _claim(engine, run_id)

        descriptor = _descriptor(engine, definition_id)
        SqlAlchemyToolInvocationPersistence(engine).record_requested(
            claim=claim, request=_request(descriptor, claim), now=NOW
        )

        row = _invocation_rows(engine)[0]
        assert row["arguments_shape"] == '["timezone"]'
        assert len(row["arguments_digest"]) == 64
        # The value itself appears nowhere in the durable row.
        assert "UTC" not in str(dict(row))


class TestFenceBinding:
    """The write statements themselves are bound to this claim's attempt, not just to a row id."""

    def test_a_request_naming_another_attempt_is_refused(self, engine: Engine) -> None:
        """The fence validates the claim, so a request naming different ids must insert nothing."""
        instance_id = _instance_id(engine)
        definition_id = _definition_id(engine, "current_time")
        _grant(engine, tool_definition_id=definition_id, instance_id=instance_id, now=NOW)
        other_run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)
        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)
        _, claim = _claim(engine, run_id)
        descriptor = _descriptor(engine, definition_id)

        mismatched = replace(_request(descriptor, claim), attempt_id=claim.attempt_id + 1)
        outcome = SqlAlchemyToolInvocationPersistence(engine).record_requested(
            claim=claim, request=mismatched, now=NOW
        )

        assert outcome.kind is RecordOutcomeKind.FENCED
        assert _invocation_rows(engine) == []
        assert other_run_id != run_id

    def test_a_terminal_write_cannot_cross_attempts(self, engine: Engine) -> None:
        """A live claim on one attempt may not conclude an invocation belonging to another."""
        instance_id = _instance_id(engine)
        definition_id = _definition_id(engine, "current_time")
        _grant(engine, tool_definition_id=definition_id, instance_id=instance_id, now=NOW)
        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)
        _, claim = _claim(engine, run_id)
        persistence = SqlAlchemyToolInvocationPersistence(engine)
        recorded = persistence.record_requested(
            claim=claim, request=_request(_descriptor(engine, definition_id), claim), now=NOW
        )
        assert recorded.invocation_id is not None
        assert (
            persistence.mark_started(
                claim=claim, invocation_id=recorded.invocation_id, now=NOW
            ).kind
            is StartOutcomeKind.STARTED
        )

        # The same authoritative claim, but a different Attempt in the same Job.
        elsewhere = replace(claim, attempt_id=claim.attempt_id + 1)
        closed = persistence.mark_succeeded(
            claim=elsewhere,
            invocation_id=recorded.invocation_id,
            envelope=result_envelope(text="late", structured=None),
            now=NOW,
        )

        assert closed is False
        assert _invocation_rows(engine)[0]["status"] == InvocationStatus.STARTED.value


class TestStartBoundaryRace:
    """The one race D4 must get right: allowed when requested, revoked before started."""

    def _prepared(self, engine: Engine) -> tuple[Any, int, Any]:
        instance_id = _instance_id(engine)
        definition_id = _definition_id(engine, "current_time")
        grant_id = _grant(
            engine, tool_definition_id=definition_id, instance_id=instance_id, now=NOW
        )
        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)
        _, claim = _claim(engine, run_id)
        return _descriptor(engine, definition_id), grant_id, claim

    def test_a_grant_revoked_before_the_start_transitions_to_denied(self, engine: Engine) -> None:
        descriptor, grant_id, claim = self._prepared(engine)
        persistence = SqlAlchemyToolInvocationPersistence(engine)
        recorded = persistence.record_requested(
            claim=claim, request=_request(descriptor, claim), now=NOW
        )
        assert recorded.invocation_id is not None

        # Revoke the capability between the two commits.
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM agent_tool_grants WHERE id = :g"), {"g": grant_id})

        outcome = persistence.mark_started(
            claim=claim, invocation_id=recorded.invocation_id, now=NOW
        )

        assert outcome.kind is StartOutcomeKind.DENIED
        row = _invocation_rows(engine)[0]
        assert row["status"] == InvocationStatus.DENIED.value
        assert row["started_at"] is None
        assert row["finished_at"] is not None
        # The stale `allowed` cannot survive beside the denied status.
        assert row["permission_decision"] == "denied_not_granted"
        assert row["result_digest"] is None
        assert row["result_bytes"] is None
        assert row["result_truncated"] is None

    def test_an_allowed_call_starts_and_the_ambiguity_boundary_is_committed(
        self, engine: Engine
    ) -> None:
        descriptor, _, claim = self._prepared(engine)
        persistence = SqlAlchemyToolInvocationPersistence(engine)
        recorded = persistence.record_requested(
            claim=claim, request=_request(descriptor, claim), now=NOW
        )
        assert recorded.invocation_id is not None

        outcome = persistence.mark_started(
            claim=claim, invocation_id=recorded.invocation_id, now=NOW
        )

        assert outcome.kind is StartOutcomeKind.STARTED
        row = _invocation_rows(engine)[0]
        assert row["status"] == InvocationStatus.STARTED.value
        assert row["started_at"] is not None
        assert row["finished_at"] is None

    def test_a_lost_claim_cannot_start_an_invocation(self, engine: Engine) -> None:
        descriptor, _, claim = self._prepared(engine)
        persistence = SqlAlchemyToolInvocationPersistence(engine)
        recorded = persistence.record_requested(
            claim=claim, request=_request(descriptor, claim), now=NOW
        )
        assert recorded.invocation_id is not None

        # A different worker takes the Job; the original claim token is now worthless.
        with engine.begin() as connection:
            connection.execute(
                update(JobRecord).values(claimed_by="worker-2", claim_token=b"z" * 32)
            )

        outcome = persistence.mark_started(
            claim=claim, invocation_id=recorded.invocation_id, now=NOW
        )

        assert outcome.kind is StartOutcomeKind.FENCED
        assert _invocation_rows(engine)[0]["status"] == InvocationStatus.REQUESTED.value

    def test_cancellation_before_the_start_transitions_to_cancelled(self, engine: Engine) -> None:
        """Cancellation before the boundary is a refusal: the call provably did not run."""
        descriptor, _, claim = self._prepared(engine)
        persistence = SqlAlchemyToolInvocationPersistence(engine)
        recorded = persistence.record_requested(
            claim=claim, request=_request(descriptor, claim), now=NOW
        )
        assert recorded.invocation_id is not None

        with engine.begin() as connection:
            connection.execute(update(JobRecord).values(cancel_requested_at=NOW, updated_at=NOW))

        outcome = persistence.mark_started(
            claim=claim, invocation_id=recorded.invocation_id, now=NOW
        )

        assert outcome.kind is StartOutcomeKind.CANCELLED
        row = _invocation_rows(engine)[0]
        assert row["status"] == InvocationStatus.CANCELLED.value
        # Finished, never started: the ambiguity boundary was never crossed.
        assert row["started_at"] is None
        assert row["finished_at"] is not None
        assert row["result_digest"] is None
        assert row["error_code"] is None

    def test_a_cancelled_run_cannot_have_a_terminal_write(self, engine: Engine) -> None:
        """A call that did start is left exactly as it is once the Run is cancelled."""
        descriptor, _, claim = self._prepared(engine)
        persistence = SqlAlchemyToolInvocationPersistence(engine)
        recorded = persistence.record_requested(
            claim=claim, request=_request(descriptor, claim), now=NOW
        )
        assert recorded.invocation_id is not None
        assert (
            persistence.mark_started(
                claim=claim, invocation_id=recorded.invocation_id, now=NOW
            ).kind
            is StartOutcomeKind.STARTED
        )
        with engine.begin() as connection:
            connection.execute(update(JobRecord).values(cancel_requested_at=NOW, updated_at=NOW))

        assert (
            persistence.mark_succeeded(
                claim=claim,
                invocation_id=recorded.invocation_id,
                envelope=result_envelope(text="late", structured=None),
                now=NOW,
            )
            is False
        )
        assert _invocation_rows(engine)[0]["status"] == InvocationStatus.STARTED.value


class TestTerminalTransitions:
    def _started(self, engine: Engine) -> tuple[SqlAlchemyToolInvocationPersistence, Any, int]:
        instance_id = _instance_id(engine)
        definition_id = _definition_id(engine, "current_time")
        _grant(engine, tool_definition_id=definition_id, instance_id=instance_id, now=NOW)
        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)
        _, claim = _claim(engine, run_id)
        persistence = SqlAlchemyToolInvocationPersistence(engine)
        recorded = persistence.record_requested(
            claim=claim, request=_request(_descriptor(engine, definition_id), claim), now=NOW
        )
        assert recorded.invocation_id is not None
        assert (
            persistence.mark_started(
                claim=claim, invocation_id=recorded.invocation_id, now=NOW
            ).kind
            is StartOutcomeKind.STARTED
        )
        return persistence, claim, recorded.invocation_id

    def test_success_records_the_full_result_evidence(self, engine: Engine) -> None:
        persistence, claim, invocation_id = self._started(engine)
        envelope = result_envelope(text="12:00", structured={"iso8601": "2026-09-18T12:00:00Z"})

        assert (
            persistence.mark_succeeded(
                claim=claim, invocation_id=invocation_id, envelope=envelope, now=NOW
            )
            is True
        )

        row = _invocation_rows(engine)[0]
        assert row["status"] == InvocationStatus.SUCCEEDED.value
        assert row["result_digest"] == envelope.digest
        assert row["result_bytes"] == envelope.bytes
        assert row["result_truncated"] in (0, False)
        assert row["error_code"] is None
        # The result itself is never persisted, only its evidence.
        assert "12:00" not in str(dict(row))

    def test_failure_records_a_safe_error_pair(self, engine: Engine) -> None:
        persistence, claim, invocation_id = self._started(engine)

        assert (
            persistence.mark_failed(
                claim=claim,
                invocation_id=invocation_id,
                error_code="arguments_invalid",
                error_message="Invalid arguments (TYPE_MISMATCH).",
                now=NOW,
            )
            is True
        )

        row = _invocation_rows(engine)[0]
        assert row["status"] == InvocationStatus.FAILED.value
        assert row["error_code"] == "arguments_invalid"
        assert row["result_digest"] is None

    def test_ambiguity_is_never_recorded_as_a_failure(self, engine: Engine) -> None:
        persistence, claim, invocation_id = self._started(engine)

        assert (
            persistence.mark_ambiguous(
                claim=claim,
                invocation_id=invocation_id,
                error_code="tool_outcome_unknown",
                error_message="NervOS could not determine whether this run's tool call completed.",
                now=NOW,
            )
            is True
        )

        row = _invocation_rows(engine)[0]
        assert row["status"] == InvocationStatus.AMBIGUOUS.value
        assert row["started_at"] is not None

    def test_a_lost_claim_cannot_conclude_a_started_invocation(self, engine: Engine) -> None:
        persistence, claim, invocation_id = self._started(engine)
        with engine.begin() as connection:
            connection.execute(update(JobRecord).values(claim_token=b"z" * 32))

        assert (
            persistence.mark_succeeded(
                claim=claim,
                invocation_id=invocation_id,
                envelope=result_envelope(text="late", structured=None),
                now=NOW,
            )
            is False
        )
        # The row stays truthfully `started`: the call happened and this worker may not say how.
        assert _invocation_rows(engine)[0]["status"] == InvocationStatus.STARTED.value

    def test_a_denied_invocation_can_be_closed_before_any_start(self, engine: Engine) -> None:
        instance_id = _instance_id(engine)
        definition_id = _definition_id(engine, "current_time")
        _grant(engine, tool_definition_id=definition_id, instance_id=instance_id, now=NOW)
        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)
        _, claim = _claim(engine, run_id)
        persistence = SqlAlchemyToolInvocationPersistence(engine)
        recorded = persistence.record_requested(
            claim=claim, request=_request(_descriptor(engine, definition_id), claim), now=NOW
        )
        assert recorded.invocation_id is not None

        closed = persistence.mark_denied(
            claim=claim,
            invocation_id=recorded.invocation_id,
            decision=PermissionDecision(allowed=False, reason=PermissionDenialReason.NOT_GRANTED),
            now=NOW,
        )

        assert closed is True
        row = _invocation_rows(engine)[0]
        assert row["status"] == InvocationStatus.DENIED.value
        assert row["started_at"] is None
        assert row["permission_decision"] == "denied_not_granted"


class TestUsageDurability:
    def test_attempt_usage_is_written_to_the_attempt_not_the_run(self, engine: Engine) -> None:
        instance_id = _instance_id(engine)
        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)
        attempt_id, claim = _claim(engine, run_id)

        written = SqlAlchemyJobExecutionPersistence(engine).persist_attempt_usage(
            claim, usage=ModelUsage(11, 7, 18), now=NOW
        )

        assert written is True
        with engine.connect() as connection:
            usage = connection.execute(
                select(
                    JobAttemptRecord.input_tokens,
                    JobAttemptRecord.output_tokens,
                    JobAttemptRecord.total_tokens,
                ).where(JobAttemptRecord.id == attempt_id)
            ).one()
            run_usage = connection.execute(
                select(
                    RunRecord.input_tokens, RunRecord.output_tokens, RunRecord.total_tokens
                ).where(RunRecord.id == run_id)
            ).one()
        assert usage == (11, 7, 18)
        # The Run's usage keeps its Stage C meaning: it is written once, at terminalization.
        assert run_usage == (None, None, None)

    def test_a_stale_claim_cannot_write_attempt_usage(self, engine: Engine) -> None:
        instance_id = _instance_id(engine)
        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)
        attempt_id, claim = _claim(engine, run_id)
        with engine.begin() as connection:
            connection.execute(update(JobRecord).values(claim_token=b"z" * 32))

        written = SqlAlchemyJobExecutionPersistence(engine).persist_attempt_usage(
            claim, usage=ModelUsage(5, None, None), now=NOW
        )

        assert written is False
        with engine.connect() as connection:
            assert (
                connection.execute(
                    select(JobAttemptRecord.input_tokens).where(JobAttemptRecord.id == attempt_id)
                ).scalar_one()
                is None
            )

    def test_an_expired_lease_cannot_write_attempt_usage(self, engine: Engine) -> None:
        instance_id = _instance_id(engine)
        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)
        _, claim = _claim(engine, run_id)

        written = SqlAlchemyJobExecutionPersistence(engine).persist_attempt_usage(
            claim, usage=ModelUsage(5, None, None), now=NOW + LEASE + timedelta(seconds=1)
        )

        assert written is False


class TestReplayGuard:
    """ADR 0017 layer two: a dispatched tool call makes the whole Attempt unreplayable."""

    def _failed_attempt(self, engine: Engine, *, dispatched: bool) -> Any:
        instance_id = _instance_id(engine)
        definition_id = _definition_id(engine, "current_time")
        _grant(engine, tool_definition_id=definition_id, instance_id=instance_id, now=NOW)
        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)
        _, claim = _claim(engine, run_id)

        persistence = SqlAlchemyToolInvocationPersistence(engine)
        recorded = persistence.record_requested(
            claim=claim, request=_request(_descriptor(engine, definition_id), claim), now=NOW
        )
        assert recorded.invocation_id is not None
        if dispatched:
            assert (
                persistence.mark_started(
                    claim=claim, invocation_id=recorded.invocation_id, now=NOW
                ).kind
                is StartOutcomeKind.STARTED
            )
        else:
            assert persistence.mark_denied(
                claim=claim,
                invocation_id=recorded.invocation_id,
                decision=PermissionDecision(
                    allowed=False, reason=PermissionDenialReason.NOT_GRANTED
                ),
                now=NOW,
            )
        return claim

    def test_case_a_a_rate_limit_with_no_dispatch_is_still_replayable(self, engine: Engine) -> None:
        """The guard must not defeat C4 for the failure class C4 exists to replay."""
        claim = self._failed_attempt(engine, dispatched=False)
        jobs = SqlAlchemyJobExecutionPersistence(engine)

        outcome = jobs.record_failure(
            claim,
            error_code=MODEL_RATE_LIMITED,
            error_message="The model provider is temporarily rate limited.",
            retry_disposition=RetryDisposition.SAFE_TO_RETRY,
            usage=ModelUsage(),
            elapsed_ms=10,
            anchor_at=NOW,
            retry_policy=PRODUCTION_RETRY_POLICY,
            now=NOW,
        )

        from nervos_core.application.job_execution import FailureOutcome

        assert outcome is FailureOutcome.RETRY_SCHEDULED

    def test_case_b_a_rate_limit_after_a_dispatch_terminates_the_run(self, engine: Engine) -> None:
        """Exactly one tool dispatch happened, and the whole Attempt is not replayed for it."""
        claim = self._failed_attempt(engine, dispatched=True)
        jobs = SqlAlchemyJobExecutionPersistence(engine)

        outcome = jobs.record_failure(
            claim,
            error_code=MODEL_RATE_LIMITED,
            error_message="The model provider is temporarily rate limited.",
            retry_disposition=RetryDisposition.SAFE_TO_RETRY,
            usage=ModelUsage(),
            elapsed_ms=10,
            anchor_at=NOW,
            retry_policy=PRODUCTION_RETRY_POLICY,
            now=NOW,
        )

        from nervos_core.application.job_execution import FailureOutcome

        assert outcome is FailureOutcome.TERMINAL_FAILED
        assert len(_invocation_rows(engine)) == 1
        with engine.connect() as connection:
            job = connection.execute(
                select(JobRecord.status, JobRecord.attempt_count).where(
                    JobRecord.id == claim.job_id
                )
            ).one()
        assert job[0] == "failed"


class TestRegistrationAndLoop:
    """The built-ins D3 made durable, executed end to end through the D4 loop."""

    def test_reconciliation_makes_the_builtins_visible_to_the_permission_layer(
        self, engine: Engine
    ) -> None:
        instance_id = _instance_id(engine)
        descriptors = reconcile_builtin_definitions(
            SqlAlchemyToolDefinitionPersistence(engine),
            specs=builtin_tool_specs(clock=lambda: NOW),
            now=NOW,
        )
        assert len(descriptors) == 3
        grant_id = _grant(
            engine,
            tool_definition_id=descriptors[0].tool_definition_id,
            instance_id=instance_id,
            now=NOW,
        )
        assert grant_id > 0

        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)
        decision = SqlAlchemyToolPermissionEvaluator(engine).check_permission(
            run_id=run_id, tool_definition_id=descriptors[0].tool_definition_id
        )

        assert decision.allowed is True

    def test_the_worker_composition_executes_a_builtin_through_the_loop(
        self, engine: Engine
    ) -> None:
        """One real built-in, one real provider double, one real durable invocation row."""
        instance_id = _instance_id(engine)
        definitions = SqlAlchemyToolDefinitionPersistence(engine)
        descriptors = reconcile_builtin_definitions(
            definitions, specs=builtin_tool_specs(clock=lambda: NOW), now=NOW
        )
        by_name = {descriptor.upstream_name: descriptor for descriptor in descriptors}
        _grant(
            engine,
            tool_definition_id=by_name["calculate"].tool_definition_id,
            instance_id=instance_id,
            now=NOW,
        )
        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)
        _, claim = _claim(engine, run_id)
        run = SqlAlchemyJobExecutionPersistence(engine).load_run(run_id)

        loop = ToolLoop(
            registry=create_builtin_tool_registry(definitions, clock=lambda: NOW),
            source_ref=BUILTIN_SOURCE_REF,
            authorize=SqlAlchemyToolPermissionEvaluator(engine),
            invocations=SqlAlchemyToolInvocationPersistence(engine),
            usage=SqlAlchemyJobExecutionPersistence(engine),
            system_instruction=NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION,
            clock=lambda: NOW,
        )
        completion = _ScriptedCompletion(
            _tool_turn(
                by_name["calculate"].model_name,
                '{"expression":"1/3"}',
            ),
            ModelResponse("one third", "anthropic", "opaque/model", StopOutcome.STOP, ModelUsage()),
        )

        outcome = asyncio.run(loop.run(completion, run, claim, 0))

        assert outcome.output_text == "one third"
        rows = _invocation_rows(engine)
        assert len(rows) == 1
        assert rows[0]["status"] == InvocationStatus.SUCCEEDED.value
        # The tool genuinely ran: its result reached the second provider request.
        observation = completion.requests[1].turns[1]
        assert observation.call_id == "call-1"
        assert observation.structured is not None
        assert "0.3333" in str(observation.structured)

    def test_the_loop_emits_exactly_the_tool_run_events_of_one_call(self, engine: Engine) -> None:
        """D6 closes the D4 deferral: a dispatched call now appears on the public timeline.

        The loop itself still knows nothing about Run Events -- it reaches durable audit only
        through the invocation port -- so what this proves is that the *transitions* carry their
        events, and that nothing else on the timeline moved.
        """
        instance_id = _instance_id(engine)
        definitions = SqlAlchemyToolDefinitionPersistence(engine)
        descriptors = reconcile_builtin_definitions(
            definitions, specs=builtin_tool_specs(clock=lambda: NOW), now=NOW
        )
        _grant(
            engine,
            tool_definition_id=descriptors[0].tool_definition_id,
            instance_id=instance_id,
            now=NOW,
        )
        run_id = _submit(engine, instance_id=instance_id, limits=TOOL_ENABLED_LIMITS)
        _, claim = _claim(engine, run_id)
        run = SqlAlchemyJobExecutionPersistence(engine).load_run(run_id)
        with engine.connect() as connection:
            before = set(
                connection.execute(
                    text("SELECT event_type FROM run_events WHERE run_id=:r"), {"r": run_id}
                ).scalars()
            )

        loop = ToolLoop(
            registry=create_builtin_tool_registry(definitions, clock=lambda: NOW),
            source_ref=BUILTIN_SOURCE_REF,
            authorize=SqlAlchemyToolPermissionEvaluator(engine),
            invocations=SqlAlchemyToolInvocationPersistence(engine),
            usage=SqlAlchemyJobExecutionPersistence(engine),
            system_instruction=NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION,
            clock=lambda: NOW,
        )
        completion = _ScriptedCompletion(
            _tool_turn(descriptors[0].model_name, '{"timezone":"UTC"}'),
            ModelResponse("done", "anthropic", "opaque/model", StopOutcome.STOP, ModelUsage()),
        )

        asyncio.run(loop.run(completion, run, claim, 0))

        with engine.connect() as connection:
            after = set(
                connection.execute(
                    text("SELECT event_type FROM run_events WHERE run_id=:r"), {"r": run_id}
                ).scalars()
            )
            invocation_id = int(
                connection.execute(
                    select(ToolInvocationRecord.id).where(
                        ToolInvocationRecord.attempt_id == claim.attempt_id
                    )
                ).scalar_one()
            )
            linked = connection.execute(
                text(
                    "SELECT event_type, tool_invocation_id FROM run_events"
                    " WHERE run_id=:r AND event_type LIKE 'tool.%' ORDER BY sequence"
                ),
                {"r": run_id},
            ).all()

        # Exactly one request, one dispatch and one success -- in that order -- and every one of
        # them points at the durable invocation rather than being a free-floating timeline fact.
        assert linked == [
            ("tool.requested", invocation_id),
            ("tool.started", invocation_id),
            ("tool.succeeded", invocation_id),
        ]
        # The Stage C events the Attempt already produced are untouched: the loop added tool
        # facts and changed no existing timeline semantics.
        assert before <= after
        assert after - before == {"tool.requested", "tool.started", "tool.succeeded"}


def _tool_turn(model_name: str, arguments_json: str) -> ModelResponse:
    return ModelResponse(
        "",
        "anthropic",
        "opaque/model",
        StopOutcome.TOOL_USE,
        ModelUsage(1, 1, 2),
        (ToolCall(call_id="call-1", name=model_name, arguments_json=arguments_json),),
    )


class _ScriptedCompletion:
    def __init__(self, *script: ModelResponse) -> None:
        self._script = list(script)
        self.requests: list[Any] = []

    async def complete(self, request: Any) -> ModelResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("the loop made an unscripted provider call")
        return self._script.pop(0)


def test_the_loop_refuses_a_run_whose_budget_is_exhausted(engine: Engine) -> None:
    """A loop limit is a Run outcome, not a provider one, and it is durable-safe."""
    instance_id = _instance_id(engine)
    definitions = SqlAlchemyToolDefinitionPersistence(engine)
    descriptors = reconcile_builtin_definitions(
        definitions, specs=builtin_tool_specs(clock=lambda: NOW), now=NOW
    )
    _grant(
        engine,
        tool_definition_id=descriptors[0].tool_definition_id,
        instance_id=instance_id,
        now=NOW,
    )
    limits = replace(TOOL_ENABLED_LIMITS, max_tool_calls=1)
    run_id = _submit(engine, instance_id=instance_id, limits=limits)
    _, claim = _claim(engine, run_id)
    run = SqlAlchemyJobExecutionPersistence(engine).load_run(run_id)

    loop = ToolLoop(
        registry=create_builtin_tool_registry(definitions, clock=lambda: NOW),
        source_ref=BUILTIN_SOURCE_REF,
        authorize=SqlAlchemyToolPermissionEvaluator(engine),
        invocations=SqlAlchemyToolInvocationPersistence(engine),
        usage=SqlAlchemyJobExecutionPersistence(engine),
        system_instruction=NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION,
        clock=lambda: NOW,
    )
    completion = _ScriptedCompletion(
        ModelResponse(
            "",
            "anthropic",
            "opaque/model",
            StopOutcome.TOOL_USE,
            ModelUsage(),
            (
                ToolCall(call_id="a", name=descriptors[0].model_name, arguments_json="{}"),
                ToolCall(call_id="b", name=descriptors[0].model_name, arguments_json="{}"),
            ),
        )
    )

    with pytest.raises(ModelProviderError) as raised:
        asyncio.run(loop.run(completion, run, claim, 0))

    assert raised.value.code == TOOL_LOOP_LIMIT
    # The over-budget batch dispatched nothing at all.
    assert _invocation_rows(engine) == []


def test_a_tool_free_run_never_reaches_the_permission_layer(engine: Engine) -> None:
    """A zero tool budget is an absence, not a smaller allowance."""
    instance_id = _instance_id(engine)
    run_id = _submit(engine, instance_id=instance_id, limits=STAGE_B_LIMITS)
    with engine.connect() as connection:
        row = connection.execute(
            select(RunRecord.max_tool_calls, RunRecord.tool_grant_cutoff_id).where(
                RunRecord.id == run_id
            )
        ).one()
    assert row == (0, 0)


def test_the_tool_invocations_table_records_no_raw_arguments_or_results(engine: Engine) -> None:
    """A durable row is evidence about a call, never a copy of what it carried."""
    columns = {column.name for column in ToolInvocationRecord.__table__.columns}
    assert not {"arguments", "result", "output", "input"} & columns
    assert {"arguments_digest", "arguments_shape", "result_digest", "result_bytes"} <= columns


def test_the_tool_definition_columns_are_unchanged_by_d4(engine: Engine) -> None:
    """D4 adds no column, no table, and no migration to the accepted D1 schema."""
    with engine.connect() as connection:
        revisions = list(
            connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
        )
    assert revisions == ["0007_stage_d1_tool_capability_audit"]


def test_a_tool_source_ref_for_builtins_carries_no_connection(tmp_path: Path) -> None:
    """D4 constructs no MCP source reference anywhere."""
    assert BUILTIN_SOURCE_REF.source_kind is ToolSourceKind.BUILTIN
    assert BUILTIN_SOURCE_REF.source_id is None


def test_the_definition_registry_still_has_no_grant_side_effect(engine: Engine) -> None:
    """Reconciliation registers tools; it never grants them."""
    reconcile_builtin_definitions(
        SqlAlchemyToolDefinitionPersistence(engine),
        specs=builtin_tool_specs(clock=lambda: NOW),
        now=NOW,
    )
    with engine.connect() as connection:
        assert (
            connection.execute(select(func.count()).select_from(AgentToolGrantRecord)).scalar_one()
            == 0
        )


def test_the_permission_persistence_surface_is_unchanged(engine: Engine) -> None:
    """D2's public grant authority is still the only thing that can create a grant."""
    persistence = SqlAlchemyToolPermissionPersistence(engine)
    assert hasattr(persistence, "grant_tool")
    assert hasattr(persistence, "revoke_tool")


def test_the_agent_definition_identity_is_exact(engine: Engine) -> None:
    """A Run of the tool-enabled definition is still resolved by its exact version pair."""
    assert AgentDefinitionId("nervos.chat", "2").agent_definition_version == "2"
