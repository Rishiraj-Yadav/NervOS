"""Durable C2 Run submission, Job claim, lease, terminalization, and closeout persistence.

The class is split along the control-plane/execution-plane boundary, so the API is
*structurally* incapable of claiming, starting, or terminalizing rather than merely
conventionally forbidden from doing so:

* `SqlAlchemyJobPersistence` — submission and its atomic capacity predicate. Composed by the
  API only.
* `SqlAlchemyJobExecutionPersistence` — claim, start, heartbeat, terminalize, and the
  operator-invoked legacy closeout. Composed by the Worker only.

Every mutation runs through one short `BEGIN IMMEDIATE` transaction on its own connection.
Events are appended through a transaction-local primitive that never opens a second
transaction and never commits.

Failure classification follows the isolated SQLite validation spike exactly: only a
busy/locked failure is treated as provably-uncommitted and therefore replayable, and a
connection is always *closed* rather than merely rolled back, because after a failed COMMIT
SQLAlchemy's bookkeeping no longer believes a transaction is open while SQLite still holds
the write lock.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import ColumnElement, Connection, Engine, and_, func, insert, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from nervos_core.application.agents import DurableSubmissionRejected
from nervos_core.application.errors import (
    PersistenceContention,
    PersistenceUnavailable,
    QueueCapacityExceeded,
)
from nervos_core.application.job_execution import (
    CancellationOutcome,
    ClaimedAttempt,
    ClaimState,
    FailureOutcome,
)
from nervos_core.application.lease_reclamation import (
    ReclaimedClaim,
    ReclamationKind,
    WorkerLiveness,
    WorkerSnapshot,
    classify_worker,
)
from nervos_core.application.model_completion import (
    EXECUTION_CANCELLED,
    EXECUTION_OUTCOME_AMBIGUOUS,
    MODEL_RATE_LIMITED,
    safe_error_message,
)
from nervos_core.application.queue_policy import PRODUCTION_QUEUE_POLICY, QueuePolicy
from nervos_core.application.retry_policy import RetryPolicy, retry_due_at
from nervos_core.application.tool_invocations import DISPATCHED_STATUSES, ClaimHandle
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.jobs import (
    AttemptStatus,
    JobStatus,
    RetryDisposition,
    RunEvent,
    RunEventType,
)
from nervos_core.domain.runs import (
    WORKER_RECOVERY_EXHAUSTED,
    ModelUsage,
    Run,
    RunLimits,
    RunStatus,
    validate_error_message,
    validate_input_text,
)
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    AgentToolGrantRecord,
    JobAttemptRecord,
    JobRecord,
    QueuePartitionRecord,
    RunEventRecord,
    RunRecord,
    ToolInvocationRecord,
    WorkerRecord,
)

# SQLite primary result codes. Classification uses the driver's own numeric code rather than
# matching on error strings, so a locale or driver change cannot silently reclassify a fault.
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6
_PRIMARY_RESULT_CODE_MASK = 0xFF

# Bounded retry for a bus/locked transaction that provably committed nothing.
_TRANSACTION_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (0.05, 0.1)

# Job states that occupy durable queue capacity. C2 reserved `retry_wait` here before anything
# wrote it, so C4 activating that state cannot raise the pending ceiling by accident.
_OCCUPYING_STATUSES = (
    JobStatus.QUEUED.value,
    JobStatus.CLAIMED.value,
    JobStatus.RUNNING.value,
    JobStatus.RETRY_WAIT.value,
)

# The two durable states a claim may legitimately be taken from, paired with the Run state each
# one must be consistent with. A `queued` Job belongs to a Run that never started; a
# `retry_wait` Job belongs to a Run that already crossed the execution-start boundary. Pairing
# them here keeps a malformed Job/Run combination out of the queue instead of letting it claim.
_CLAIMABLE_SOURCES = (
    (JobStatus.QUEUED, RunStatus.CREATED),
    (JobStatus.RETRY_WAIT, RunStatus.RUNNING),
)

# Job states that consume execution concurrency. A live lease is execution authority, so a
# `claimed` Job that has not started yet still occupies a slot. `retry_wait` holds no lease and
# `queued` has never been claimed, so neither consumes concurrency.
_ACTIVE_STATUSES = (JobStatus.CLAIMED.value, JobStatus.RUNNING.value)

_MIN_PENDING_CAP = 1
_MAX_PENDING_CAP = 100_000


def _validate_pending_cap(value: int, name: str) -> int:
    """Reject a pending cap outside the accepted bound, naming the offending dimension."""
    if not _MIN_PENDING_CAP <= value <= _MAX_PENDING_CAP:
        raise ValueError(f"{name} must be between {_MIN_PENDING_CAP} and {_MAX_PENDING_CAP}")
    return value


_T = TypeVar("_T")


class _Fenced(RuntimeError):
    """A fenced update affected zero rows, so the whole transaction must roll back."""


def run_from_record(
    record: RunRecord,
    *,
    execution_phase: JobStatus | None = None,
    retry_available_at: datetime | None = None,
) -> Run:
    """Map one persisted Run row onto the immutable domain value.

    `execution_phase` and `retry_available_at` are supplied only by the owner-scoped read paths,
    which join the one Job behind the Run. They are passed in rather than looked up here so this
    mapper stays a pure row translation with no second query, and so every existing caller that
    has no Job in hand keeps working unchanged.
    """
    limits = RunLimits(
        record.input_max_bytes,
        record.input_max_code_points,
        record.output_max_bytes,
        record.output_max_code_points,
        record.provider_timeout_ms,
        record.max_output_tokens,
        record.max_model_calls,
        record.max_tool_calls,
        record.tool_timeout_ms,
        record.tool_result_max_bytes,
        record.max_consecutive_tool_failures,
    )
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
        ModelUsage(record.input_tokens, record.output_tokens, record.total_tokens),
        record.elapsed_ms,
        execution_phase,
        retry_available_at,
        record.tool_grant_cutoff_id,
    )


def run_event_from_record(record: RunEventRecord) -> RunEvent:
    """Map one persisted Run Event row onto the immutable domain value.

    The row is the whole fact: C1 stores no payload, token, lease, worker identity, or provider
    detail, so reading it back is a field-for-field translation with nothing to decide.
    """
    return RunEvent(
        record.id,
        record.run_id,
        record.job_id,
        record.attempt_id,
        record.sequence,
        RunEventType(record.event_type),
        record.code,
        record.message,
        record.attempt_number,
        record.available_at,
        record.created_at,
    )


def read_run_record(engine: Engine, run_id: int) -> RunRecord | None:
    """Read one Run row through a short ORM session.

    Executing `select(RunRecord)` through a raw `Connection` yields column rows rather than
    ORM instances, so every path that needs a mapped record reads it through a session.
    """
    with Session(engine) as session:
        return session.scalar(select(RunRecord).where(RunRecord.id == run_id))


def _is_contention(error: SQLAlchemyError) -> bool:
    """Return whether the driver proved this failure committed nothing.

    Only `SQLITE_BUSY` and `SQLITE_LOCKED` qualify: a busy `BEGIN IMMEDIATE` never opened a
    transaction, and a busy statement or `COMMIT` leaves the transaction open with nothing
    committed. Any other failure is uncertain and is never replayed automatically.
    """
    code = getattr(getattr(error, "orig", None), "sqlite_errorcode", None)
    if not isinstance(code, int):
        return False
    return code & _PRIMARY_RESULT_CODE_MASK in (_SQLITE_BUSY, _SQLITE_LOCKED)


def _sequence_base(connection: Connection, run_id: int) -> int:
    """Read the per-Run event high-water mark exactly once."""
    return int(
        connection.scalar(
            select(func.coalesce(func.max(RunEventRecord.sequence), 0)).where(
                RunEventRecord.run_id == run_id
            )
        )
        or 0
    )


def _append_event_on_connection(
    connection: Connection,
    *,
    run_id: int,
    job_id: int,
    sequence: int,
    event_type: RunEventType,
    created_at: datetime,
    attempt_id: int | None = None,
    code: str | None = None,
    message: str | None = None,
    attempt_number: int | None = None,
    available_at: datetime | None = None,
) -> None:
    """Insert one Run Event on the caller's connection without opening or committing a transaction.

    C1's standalone appender opens its own connection and its own `BEGIN IMMEDIATE`, so
    calling it from inside an open lifecycle transaction would attempt a nested, competing
    SQLite write transaction. Every C2 mutation therefore appends its events through this
    primitive, inside the same transaction that performed the mutation.
    """
    connection.execute(
        insert(RunEventRecord).values(
            run_id=run_id,
            job_id=job_id,
            attempt_id=attempt_id,
            sequence=sequence,
            event_type=event_type.value,
            code=code,
            message=message,
            attempt_number=attempt_number,
            available_at=available_at,
            created_at=created_at,
        )
    )


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def _elapsed_ms(started_at: datetime | None, now: datetime) -> int:
    if started_at is None:
        return 0
    return max(0, int((now - started_at).total_seconds() * 1000))


class _TransactionRunner:
    """One short `BEGIN IMMEDIATE` transaction per operation, with proven-safe retry only."""

    def __init__(self, engine: Engine, sleep: Callable[[float], None]) -> None:
        self._engine = engine
        self._sleep = sleep

    def run(
        self,
        operation: Callable[[Connection], _T],
        *,
        attempts: int = _TRANSACTION_ATTEMPTS,
    ) -> _T:
        last_error: SQLAlchemyError | None = None
        for index in range(attempts):
            connection = self._engine.connect()
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                result = operation(connection)
                connection.commit()
                return result
            except SQLAlchemyError as error:
                with contextlib.suppress(SQLAlchemyError):
                    connection.rollback()
                if not _is_contention(error):
                    raise PersistenceUnavailable from error
                last_error = error
            except BaseException:
                with contextlib.suppress(SQLAlchemyError):
                    connection.rollback()
                raise
            finally:
                # `close()` is the load-bearing step, not `rollback()`. After a failed COMMIT
                # SQLAlchemy no longer believes a transaction is open, so `rollback()` is a
                # no-op while SQLite still holds the lock; returning the connection to the
                # pool performs the real driver-level rollback and releases it. Leaving it
                # open would block every other reader on this database.
                connection.close()
            if index + 1 < attempts:
                self._sleep(_RETRY_BACKOFF_SECONDS[min(index, len(_RETRY_BACKOFF_SECONDS) - 1)])
        # The retry budget is spent and every failure was a busy/locked contention, which
        # proves nothing committed. This is the one failure class a caller may replay.
        raise PersistenceContention from last_error


@dataclass
class _SubmissionProgress:
    """What the submission transaction had already done when it failed."""

    run_id: int | None = None


class SqlAlchemyJobPersistence:
    """Control-plane-only durable submission: Run + Job + initial events, atomically."""

    def __init__(
        self,
        engine: Engine,
        *,
        max_pending: int = 1000,
        max_pending_per_agent: int | None = None,
        max_pending_per_provider: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Bind the admission limits.

        A per-dimension limit left unset resolves to the global one, which makes it non-binding:
        a single dimension can then never exceed what the global ceiling already refuses, so C6
        adds the dimension without refusing a Run that C2 would have admitted.
        """
        self._engine = engine
        self._max_pending = _validate_pending_cap(max_pending, "max_pending")
        self._max_pending_per_agent = _validate_pending_cap(
            self._max_pending if max_pending_per_agent is None else max_pending_per_agent,
            "max_pending_per_agent",
        )
        self._max_pending_per_provider = _validate_pending_cap(
            self._max_pending if max_pending_per_provider is None else max_pending_per_provider,
            "max_pending_per_provider",
        )
        self._runner = _TransactionRunner(engine, sleep)

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
        max_pending: int | None = None,
        max_pending_per_agent: int | None = None,
        max_pending_per_provider: int | None = None,
    ) -> Run:
        """Atomically snapshot an owned enabled Instance into Run, Job, and two events."""
        validate_input_text(input_text, limits)
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        capacity = _validate_pending_cap(
            self._max_pending if max_pending is None else max_pending, "max_pending"
        )
        agent_capacity = _validate_pending_cap(
            self._max_pending_per_agent if max_pending_per_agent is None else max_pending_per_agent,
            "max_pending_per_agent",
        )
        provider_capacity = _validate_pending_cap(
            self._max_pending_per_provider
            if max_pending_per_provider is None
            else max_pending_per_provider,
            "max_pending_per_provider",
        )

        progress = _SubmissionProgress()
        try:
            return self._runner.run(
                lambda connection: self._submit_once(
                    connection,
                    owner_user_id=owner_user_id,
                    agent_instance_id=agent_instance_id,
                    input_text=input_text,
                    limits=limits,
                    definition_id=definition_id,
                    now=now,
                    max_attempts=max_attempts,
                    capacity=capacity,
                    agent_capacity=agent_capacity,
                    provider_capacity=provider_capacity,
                    progress=progress,
                )
            )
        except PersistenceUnavailable as error:
            if progress.run_id is not None:
                reconciled = self._reconcile_submission(progress.run_id)
                if reconciled is not None:
                    return reconciled
            if type(error) is PersistenceUnavailable:
                raise
            # A proven contention is normalized to the base class: the control plane must
            # never surface a "safe to replay" signal, because it does not replay.
            raise PersistenceUnavailable from error

    def _submit_once(
        self,
        connection: Connection,
        *,
        owner_user_id: int,
        agent_instance_id: int,
        input_text: str,
        limits: RunLimits,
        definition_id: AgentDefinitionId | None,
        now: datetime,
        max_attempts: int,
        capacity: int,
        agent_capacity: int,
        provider_capacity: int,
        progress: _SubmissionProgress,
    ) -> Run:
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

        # All three admission dimensions are counted inside the same `BEGIN IMMEDIATE`
        # transaction that inserts the Job, so two concurrent submissions serialize on the write
        # lock: the second reads the first's committed Job rather than racing it. One grouped
        # read produces every dimension at one consistent database state, so the three checks
        # cannot disagree with each other and nothing is counted twice.
        occupying = connection.execute(
            select(
                JobRecord.agent_instance_id,
                JobRecord.model_provider,
                func.count(),
            )
            .where(JobRecord.status.in_(_OCCUPYING_STATUSES))
            .group_by(JobRecord.agent_instance_id, JobRecord.model_provider)
        ).all()
        pending = sum(int(count) for _agent, _provider, count in occupying)
        if pending >= capacity:
            raise QueueCapacityExceeded
        provider_id = str(instance["model_provider"])
        agent_pending = sum(
            int(count) for owner, _provider, count in occupying if int(owner) == agent_instance_id
        )
        if agent_pending >= agent_capacity:
            raise QueueCapacityExceeded
        provider_pending = sum(
            int(count) for _owner, provider, count in occupying if str(provider) == provider_id
        )
        if provider_pending >= provider_capacity:
            raise QueueCapacityExceeded

        tool_grant_cutoff_id = self._grant_cutoff_on_connection(
            connection, agent_instance_id, limits
        )
        run_result = connection.execute(
            insert(RunRecord).values(
                agent_instance_id=agent_instance_id,
                status=RunStatus.CREATED.value,
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
                max_tool_calls=limits.max_tool_calls,
                tool_timeout_ms=limits.tool_timeout_ms,
                tool_result_max_bytes=limits.tool_result_max_bytes,
                max_consecutive_tool_failures=limits.max_consecutive_tool_failures,
                tool_grant_cutoff_id=tool_grant_cutoff_id,
                created_at=now,
            )
        )
        run_pk = run_result.inserted_primary_key
        if run_pk is None or run_pk[0] is None:
            raise PersistenceUnavailable
        run_id = int(run_pk[0])
        progress.run_id = run_id

        job_result = connection.execute(
            insert(JobRecord).values(
                run_id=run_id,
                agent_instance_id=agent_instance_id,
                model_provider=instance["model_provider"],
                status=JobStatus.QUEUED.value,
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

        _append_event_on_connection(
            connection,
            run_id=run_id,
            job_id=job_id,
            sequence=1,
            event_type=RunEventType.RUN_CREATED,
            created_at=now,
        )
        _append_event_on_connection(
            connection,
            run_id=run_id,
            job_id=job_id,
            sequence=2,
            event_type=RunEventType.RUN_QUEUED,
            created_at=now,
            available_at=now,
        )
        return Run(
            run_id,
            agent_instance_id,
            instance["agent_key"],
            instance["agent_definition_version"],
            instance["model_provider"],
            instance["model_name"],
            input_text,
            limits,
            RunStatus.CREATED,
            now,
            execution_phase=JobStatus.QUEUED,
            tool_grant_cutoff_id=tool_grant_cutoff_id,
        )

    @staticmethod
    def _grant_cutoff_on_connection(
        connection: Connection, agent_instance_id: int, limits: RunLimits
    ) -> int:
        """Snapshot the highest grant id this Run may ever honour.

        Read inside the same serialized submission transaction that inserts the Run, so a grant
        committed a moment later is ordered *after* this Run rather than racing it. A tool-free Run
        snapshots nothing: its budget of zero tools means no capability is reachable at all, which
        is exactly the `0` D2's evaluator already refuses everything against. That is why chat@1
        needs no special case and keeps the durable values it has always had.
        """
        if limits.max_tool_calls == 0:
            return 0
        highest = connection.execute(
            select(func.coalesce(func.max(AgentToolGrantRecord.id), 0)).where(
                AgentToolGrantRecord.agent_instance_id == agent_instance_id
            )
        ).scalar_one()
        return int(highest)

    def _reconcile_submission(self, run_id: int) -> Run | None:
        """Read back an unobserved submission outcome instead of blindly replaying it.

        The Run identifier is known because the insert had already executed when the failure
        happened, so this is an exact read rather than a guess. A Run that is present and
        complete is treated as committed; a Run with missing children is an invariant
        failure and is never replayed; an absent Run means nothing committed.
        """
        with self._engine.connect() as connection:
            job = (
                connection.execute(
                    select(JobRecord.id, JobRecord.status, JobRecord.available_at)
                    .where(JobRecord.run_id == run_id)
                    .order_by(JobRecord.id)
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            sequences = [
                (int(sequence), event_type)
                for sequence, event_type in connection.execute(
                    select(RunEventRecord.sequence, RunEventRecord.event_type)
                    .where(RunEventRecord.run_id == run_id)
                    .order_by(RunEventRecord.sequence)
                )
            ]
        record = read_run_record(self._engine, run_id)
        if record is None:
            return None
        if job is None or sequences != [
            (1, RunEventType.RUN_CREATED.value),
            (2, RunEventType.RUN_QUEUED.value),
        ]:
            raise PersistenceUnavailable
        # The committed Job is read rather than assumed: a lost response can be reconciled after a
        # Worker has already claimed the Run, and reporting `queued` then would be a stale claim.
        phase = JobStatus(job["status"])
        return run_from_record(
            record,
            execution_phase=phase,
            retry_available_at=job["available_at"] if phase is JobStatus.RETRY_WAIT else None,
        )

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
        """Legacy standalone appender: open one transaction and delegate to the local primitive."""
        resolved = RunEventType(event_type)

        def operation(connection: Connection) -> int:
            sequence = _sequence_base(connection, run_id) + 1
            _append_event_on_connection(
                connection,
                run_id=run_id,
                job_id=job_id,
                attempt_id=attempt_id,
                sequence=sequence,
                event_type=resolved,
                code=code,
                message=message,
                attempt_number=attempt_number,
                available_at=available_at,
                created_at=created_at,
            )
            return sequence

        return self._runner.run(operation)


@dataclass
class _ClaimProgress:
    """The exact claim the transaction was attempting when it failed."""

    job_id: int
    run_id: int
    attempt_id: int | None = None
    attempt_number: int = 0
    token: bytes = b""
    lease_expires_at: datetime | None = None


class SqlAlchemyJobExecutionPersistence:
    """Execution-plane-only durable claim, lease, terminalization, and closeout."""

    def __init__(
        self,
        engine: Engine,
        *,
        policy: QueuePolicy = PRODUCTION_QUEUE_POLICY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._engine = engine
        self._policy = policy
        self._runner = _TransactionRunner(engine, sleep)

    # -- claim -------------------------------------------------------------------------

    def claim_next(
        self,
        *,
        worker_id: str,
        provider_ids: Sequence[str],
        max_active: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedAttempt | None:
        """Claim the fairest eligible Job, or return None.

        Eligibility is capability-aware: a Worker only ever claims a Job whose provider it
        has configured. An empty configured set issues no query at all rather than a
        malformed empty `IN` list.

        Selection is two-dimensional. A Job must satisfy every cap to be a candidate at all
        (global, per-Agent, per-provider), and among the surviving Agent partitions the
        least-recently-served one wins. A partition that is saturated, or whose only work is on a
        provider this Worker cannot run, is removed from the candidate set *before* selection
        rather than after it, which is what keeps a blocked partition from head-of-line blocking
        an eligible one.
        """
        if not provider_ids:
            return None
        token = os.urandom(32)
        lease_expires_at = now + lease_duration
        progress = _ClaimProgress(job_id=0, run_id=0)
        try:
            return self._runner.run(
                lambda connection: self._claim_once(
                    connection,
                    worker_id=worker_id,
                    provider_ids=tuple(provider_ids),
                    max_active=max_active,
                    now=now,
                    lease_expires_at=lease_expires_at,
                    token=token,
                    progress=progress,
                )
            )
        except _Fenced:
            # The atomic compare-and-set lost a race: another Worker owns this Job now.
            return None
        except PersistenceUnavailable:
            if progress.job_id <= 0:
                raise
            return self._reconcile_claim(progress, worker_id=worker_id, token=token, now=now)

    def _claim_once(
        self,
        connection: Connection,
        *,
        worker_id: str,
        provider_ids: tuple[str, ...],
        max_active: int,
        now: datetime,
        lease_expires_at: datetime,
        token: bytes,
        progress: _ClaimProgress,
    ) -> ClaimedAttempt | None:
        # Live leases occupy execution concurrency: a lease is execution authority, so a
        # `claimed` Job that has not started yet still holds its slot. An expired lease holds no
        # authority, so it frees capacity immediately -- and that cannot authorize a second
        # execution of the same Job, because an expired Job is still `claimed`/`running` and so
        # is unreachable from `_CLAIMABLE_SOURCES`; only C3 reclamation can requeue it. One
        # grouped read yields all three dimensions at one consistent database state.
        live = connection.execute(
            select(JobRecord.agent_instance_id, JobRecord.model_provider, func.count())
            .where(
                JobRecord.status.in_(_ACTIVE_STATUSES),
                JobRecord.lease_expires_at > now,
            )
            .group_by(JobRecord.agent_instance_id, JobRecord.model_provider)
        ).all()

        # A Worker's own budget may only tighten the global limit, never raise it, so a fleet
        # whose Workers were configured differently still respects the authoritative ceiling.
        if sum(int(count) for _owner, _provider, count in live) >= min(
            self._policy.global_active_limit, max_active
        ):
            return None

        active_agents: dict[int, int] = {}
        active_providers: dict[str, int] = {}
        for owner, provider, count in live:
            active_agents[int(owner)] = active_agents.get(int(owner), 0) + int(count)
            name = str(provider)
            active_providers[name] = active_providers.get(name, 0) + int(count)
        saturated_agents = sorted(
            owner
            for owner, total in active_agents.items()
            if total >= self._policy.per_agent_active_limit
        )
        saturated_providers = sorted(
            name
            for name, total in active_providers.items()
            if total >= self._policy.per_provider_active_limit
        )

        # The head Job of every eligible partition, ranked inside the partition by the accepted
        # within-partition order: oldest due first, with the stable Job id as tie-break. A due
        # `retry_wait` Job enters this ranking on exactly the same terms as queued work -- there
        # is no retry lane and no retry priority.
        ranked = (
            select(
                JobRecord.id.label("job_id"),
                JobRecord.run_id.label("run_id"),
                JobRecord.agent_instance_id.label("agent_instance_id"),
                JobRecord.model_provider.label("model_provider"),
                JobRecord.attempt_count.label("attempt_count"),
                JobRecord.status.label("status"),
                func.row_number()
                .over(
                    partition_by=JobRecord.agent_instance_id,
                    order_by=(JobRecord.available_at.asc(), JobRecord.id.asc()),
                )
                .label("rank"),
            )
            .join(RunRecord, RunRecord.id == JobRecord.run_id)
            .where(
                or_(
                    *(
                        and_(
                            JobRecord.status == job_status.value,
                            RunRecord.status == run_status.value,
                        )
                        for job_status, run_status in _CLAIMABLE_SOURCES
                    )
                ),
                JobRecord.available_at <= now,
                JobRecord.attempt_count < JobRecord.max_attempts,
                JobRecord.model_provider.in_(provider_ids),
            )
            .subquery("ranked")
        )
        heads = select(ranked).where(ranked.c.rank == 1).subquery("heads")
        exclusions: list[ColumnElement[bool]] = []
        if saturated_agents:
            exclusions.append(heads.c.agent_instance_id.not_in(saturated_agents))
        if saturated_providers:
            exclusions.append(heads.c.model_provider.not_in(saturated_providers))

        # Least-recently-served wins, read from durable per-partition metadata so the choice is
        # identical for every Worker no matter which providers it is capable of running. A
        # partition with no row, or with a NULL marker, has never been served and sorts first;
        # the Agent id breaks any remaining tie deterministically. Only a committed claim writes
        # the marker, so a poll that finds nothing, loses a CAS, or rolls back never penalizes a
        # partition's position, and a partition that was skipped re-enters at its own place.
        candidate = (
            connection.execute(
                select(
                    heads.c.job_id,
                    heads.c.run_id,
                    heads.c.agent_instance_id,
                    heads.c.attempt_count,
                    heads.c.status,
                )
                .outerjoin(
                    QueuePartitionRecord,
                    QueuePartitionRecord.agent_instance_id == heads.c.agent_instance_id,
                )
                .where(*exclusions)
                .order_by(
                    QueuePartitionRecord.last_served_attempt_id.asc().nullsfirst(),
                    heads.c.agent_instance_id.asc(),
                )
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if candidate is None:
            return None

        job_id = int(candidate["job_id"])
        run_id = int(candidate["run_id"])
        agent_instance_id = int(candidate["agent_instance_id"])
        # A due `retry_wait` Job is claimable through exactly the same path as queued work. The
        # compare-and-set below therefore pins the *observed* source status rather than
        # accepting either claimable state: a Job that changed hands between the select and the
        # update must fail its fence instead of being claimed from a shape we did not classify.
        source_status = str(candidate["status"])
        attempt_number = int(candidate["attempt_count"]) + 1
        progress.job_id = job_id
        progress.run_id = run_id
        progress.token = token
        progress.lease_expires_at = lease_expires_at

        updated = connection.execute(
            update(JobRecord)
            .where(
                JobRecord.id == job_id,
                JobRecord.status == source_status,
                JobRecord.attempt_count < JobRecord.max_attempts,
            )
            .values(
                status=JobStatus.CLAIMED.value,
                claimed_by=worker_id,
                claim_token=token,
                lease_expires_at=lease_expires_at,
                last_heartbeat_at=now,
                attempt_count=JobRecord.attempt_count + 1,
                updated_at=now,
            )
        )
        if _rowcount(updated) != 1:
            raise _Fenced

        attempt_result = connection.execute(
            insert(JobAttemptRecord)
            .values(
                job_id=job_id,
                attempt_number=attempt_number,
                status=AttemptStatus.CLAIMED.value,
                worker_id=worker_id,
                claim_token=token,
                claimed_at=now,
                execution_started_at=None,
                lease_expires_at=lease_expires_at,
                last_heartbeat_at=now,
                created_at=now,
            )
            .returning(JobAttemptRecord.id)
        )
        attempt_id = attempt_result.scalar_one_or_none()
        if attempt_id is None:
            raise PersistenceUnavailable
        progress.attempt_id = int(attempt_id)
        progress.attempt_number = attempt_number

        # Advancing the partition is part of the claim, not a follow-up: the marker moves only
        # inside this transaction, so it moves only for a claim that actually commits. A poll
        # that found nothing, lost the CAS, or rolled back leaves the partition's position
        # untouched, and the next Worker reads the same history regardless of the providers it
        # can run.
        connection.execute(
            sqlite_insert(QueuePartitionRecord)
            .values(
                agent_instance_id=agent_instance_id,
                last_served_attempt_id=int(attempt_id),
            )
            .on_conflict_do_update(
                index_elements=[QueuePartitionRecord.agent_instance_id],
                set_={"last_served_attempt_id": int(attempt_id)},
            )
        )

        _append_event_on_connection(
            connection,
            run_id=run_id,
            job_id=job_id,
            attempt_id=int(attempt_id),
            sequence=_sequence_base(connection, run_id) + 1,
            event_type=RunEventType.ATTEMPT_CLAIMED,
            attempt_number=attempt_number,
            created_at=now,
        )
        return ClaimedAttempt(
            job_id=job_id,
            run_id=run_id,
            attempt_id=int(attempt_id),
            attempt_number=attempt_number,
            worker_id=worker_id,
            claim_token=token,
            lease_expires_at=lease_expires_at,
        )

    def _reconcile_claim(
        self,
        progress: _ClaimProgress,
        *,
        worker_id: str,
        token: bytes,
        now: datetime,
    ) -> ClaimedAttempt | None:
        """Read back an unobserved claim outcome instead of blindly replaying it.

        A claim whose Job and Attempt both carry this exact token is recovered as committed.
        Anything else means this operation did not commit, and doing nothing is always safe:
        the Job stays queued for another Worker, and the caller simply polls again.
        """
        with self._engine.connect() as connection:
            job = (
                connection.execute(
                    select(
                        JobRecord.status,
                        JobRecord.claimed_by,
                        JobRecord.claim_token,
                        JobRecord.lease_expires_at,
                    ).where(JobRecord.id == progress.job_id)
                )
                .mappings()
                .one_or_none()
            )
            attempt = (
                connection.execute(
                    select(
                        JobAttemptRecord.id,
                        JobAttemptRecord.attempt_number,
                        JobAttemptRecord.worker_id,
                        JobAttemptRecord.claim_token,
                    ).where(
                        JobAttemptRecord.job_id == progress.job_id,
                        JobAttemptRecord.status.in_(
                            (AttemptStatus.CLAIMED.value, AttemptStatus.RUNNING.value)
                        ),
                    )
                )
                .mappings()
                .one_or_none()
            )
        if job is None or attempt is None:
            return None
        if (
            job["status"] not in (JobStatus.CLAIMED.value, JobStatus.RUNNING.value)
            or job["claimed_by"] != worker_id
            or job["claim_token"] != token
            or attempt["claim_token"] != token
            or attempt["worker_id"] != worker_id
            or job["lease_expires_at"] is None
        ):
            return None
        del now
        return ClaimedAttempt(
            job_id=progress.job_id,
            run_id=progress.run_id,
            attempt_id=int(attempt["id"]),
            attempt_number=int(attempt["attempt_number"]),
            worker_id=worker_id,
            claim_token=token,
            lease_expires_at=job["lease_expires_at"],
        )

    # -- reads -------------------------------------------------------------------------

    def load_run(self, run_id: int) -> Run:
        record = read_run_record(self._engine, run_id)
        if record is None:
            raise PersistenceUnavailable
        return run_from_record(record)

    def inspect_claim(self, claim: ClaimedAttempt, *, now: datetime) -> ClaimState:
        """Classify a claim after a fenced write matched zero rows."""
        with self._engine.connect() as connection:
            job = (
                connection.execute(
                    select(
                        JobRecord.status,
                        JobRecord.claimed_by,
                        JobRecord.claim_token,
                        JobRecord.lease_expires_at,
                    ).where(JobRecord.id == claim.job_id)
                )
                .mappings()
                .one_or_none()
            )
            attempt_status = connection.scalar(
                select(JobAttemptRecord.status).where(JobAttemptRecord.id == claim.attempt_id)
            )
        if job is None or attempt_status is None:
            return ClaimState.LOST
        # Cancellation is reported apart from every other terminal state: the Worker must stop
        # waiting on the provider rather than quietly let it run to its deadline.
        if (
            job["status"] == JobStatus.CANCELLED.value
            or attempt_status == AttemptStatus.CANCELLED.value
        ):
            return ClaimState.CANCELLED
        if job["status"] in (
            JobStatus.SUCCEEDED.value,
            JobStatus.FAILED.value,
        ) or attempt_status in (
            AttemptStatus.SUCCEEDED.value,
            AttemptStatus.FAILED.value,
            AttemptStatus.EXPIRED.value,
        ):
            return ClaimState.TERMINAL
        lease = job["lease_expires_at"]
        if (
            job["status"] == JobStatus.RUNNING.value
            and attempt_status == AttemptStatus.RUNNING.value
            and job["claimed_by"] == claim.worker_id
            and job["claim_token"] == claim.claim_token
            and lease is not None
            and lease > now
        ):
            return ClaimState.ACTIVE
        return ClaimState.LOST

    # -- execution boundary ------------------------------------------------------------

    def start_attempt(self, claim: ClaimedAttempt, *, now: datetime) -> bool:
        """Commit the execution-start boundary before any external call is made.

        Returns True only when the boundary is durably committed — either by this call or by an
        earlier call whose COMMIT this process never observed. A caller may therefore invoke
        the provider exactly when True is returned, and must not invoke it otherwise.

        C4 makes this the *second* start of the same Run possible. The Run's original
        `started_at` is the moment execution first began and is load-bearing evidence for the
        whole Run, so a retry start asserts that shape without rewriting it: no second start
        timestamp and no duplicate `attempt.started` for an Attempt that already started.
        """
        try:
            return bool(
                self._runner.run(lambda connection: self._start_once(connection, claim, now=now))
            )
        except _Fenced:
            return self._start_already_committed(claim, now=now)
        except (PersistenceContention, PersistenceUnavailable):
            # An uncertain COMMIT is reconciled by reading durable state, never by replaying
            # the transition blindly: a replay could append a second start event.
            return self._start_already_committed(claim, now=now)

    def _start_once(self, connection: Connection, claim: ClaimedAttempt, *, now: datetime) -> bool:
        attempt_update = connection.execute(
            update(JobAttemptRecord)
            .where(
                JobAttemptRecord.id == claim.attempt_id,
                JobAttemptRecord.job_id == claim.job_id,
                JobAttemptRecord.status == AttemptStatus.CLAIMED.value,
                JobAttemptRecord.worker_id == claim.worker_id,
                JobAttemptRecord.claim_token == claim.claim_token,
                JobAttemptRecord.lease_expires_at > now,
            )
            .values(status=AttemptStatus.RUNNING.value, execution_started_at=now)
        )
        if _rowcount(attempt_update) != 1:
            raise _Fenced
        job_update = connection.execute(
            update(JobRecord)
            .where(
                JobRecord.id == claim.job_id,
                JobRecord.status == JobStatus.CLAIMED.value,
                JobRecord.claimed_by == claim.worker_id,
                JobRecord.claim_token == claim.claim_token,
                JobRecord.lease_expires_at > now,
                # An accepted cancellation revokes the right to cross the execution boundary.
                # Cancellation terminalizes in the same transaction that records the request,
                # so this predicate is a redundant guard rather than a live race outcome --
                # and it is what makes "no provider call after an accepted cancellation"
                # checkable here instead of merely argued.
                JobRecord.cancel_requested_at.is_(None),
            )
            .values(status=JobStatus.RUNNING.value, updated_at=now)
        )
        if _rowcount(job_update) != 1:
            raise _Fenced
        # First execution of the Run: claim the `created` shape and record the real start
        # instant. Only the very first started Attempt can do this.
        run_update = connection.execute(
            update(RunRecord)
            .where(
                RunRecord.id == claim.run_id,
                RunRecord.status == RunStatus.CREATED.value,
                RunRecord.started_at.is_(None),
            )
            .values(status=RunStatus.RUNNING.value, started_at=now)
        )
        if _rowcount(run_update) != 1:
            # A retry start leaves the Run exactly as the first start left it. 'BEGIN IMMEDIATE'
            # already holds the write lock, so this read cannot race a concurrent writer.
            run = (
                connection.execute(
                    select(RunRecord.status, RunRecord.started_at).where(
                        RunRecord.id == claim.run_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if run is None or run["status"] != RunStatus.RUNNING.value or run["started_at"] is None:
                raise _Fenced
        _append_event_on_connection(
            connection,
            run_id=claim.run_id,
            job_id=claim.job_id,
            attempt_id=claim.attempt_id,
            sequence=_sequence_base(connection, claim.run_id) + 1,
            event_type=RunEventType.ATTEMPT_STARTED,
            attempt_number=claim.attempt_number,
            created_at=now,
        )
        return True

    def _start_already_committed(self, claim: ClaimedAttempt, *, now: datetime) -> bool:
        """Return whether this exact Attempt's start boundary is already durably committed.

        Requires every fact the commit would have written, including exactly one start event
        for this Attempt, so an uncommitted or partially applied start can never be mistaken
        for a committed one.
        """
        with self._engine.connect() as connection:
            attempt = (
                connection.execute(
                    select(
                        JobAttemptRecord.status,
                        JobAttemptRecord.worker_id,
                        JobAttemptRecord.claim_token,
                        JobAttemptRecord.lease_expires_at,
                        JobAttemptRecord.execution_started_at,
                    ).where(JobAttemptRecord.id == claim.attempt_id)
                )
                .mappings()
                .one_or_none()
            )
            job = (
                connection.execute(
                    select(
                        JobRecord.status,
                        JobRecord.claimed_by,
                        JobRecord.claim_token,
                        JobRecord.lease_expires_at,
                    ).where(JobRecord.id == claim.job_id)
                )
                .mappings()
                .one_or_none()
            )
            started_events = int(
                connection.scalar(
                    select(func.count())
                    .select_from(RunEventRecord)
                    .where(
                        RunEventRecord.attempt_id == claim.attempt_id,
                        RunEventRecord.event_type == RunEventType.ATTEMPT_STARTED.value,
                    )
                )
                or 0
            )
        if attempt is None or job is None:
            return False
        return (
            attempt["status"] == AttemptStatus.RUNNING.value
            and attempt["execution_started_at"] is not None
            and attempt["worker_id"] == claim.worker_id
            and attempt["claim_token"] == claim.claim_token
            and attempt["lease_expires_at"] > now
            and job["status"] == JobStatus.RUNNING.value
            and job["claimed_by"] == claim.worker_id
            and job["claim_token"] == claim.claim_token
            and job["lease_expires_at"] is not None
            and job["lease_expires_at"] > now
            and started_events == 1
        )

    def renew_lease(
        self, claim: ClaimedAttempt, *, now: datetime, lease_duration: timedelta
    ) -> bool:
        """Renew both rows of one claim; a zero-row result means authority was lost."""

        def operation(connection: Connection) -> bool:
            job_update = connection.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == claim.job_id,
                    JobRecord.status == JobStatus.RUNNING.value,
                    JobRecord.claimed_by == claim.worker_id,
                    JobRecord.claim_token == claim.claim_token,
                    JobRecord.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=now + lease_duration,
                    last_heartbeat_at=now,
                    updated_at=now,
                )
            )
            if _rowcount(job_update) != 1:
                raise _Fenced
            attempt_update = connection.execute(
                update(JobAttemptRecord)
                .where(
                    JobAttemptRecord.id == claim.attempt_id,
                    JobAttemptRecord.job_id == claim.job_id,
                    JobAttemptRecord.status == AttemptStatus.RUNNING.value,
                    JobAttemptRecord.worker_id == claim.worker_id,
                    JobAttemptRecord.claim_token == claim.claim_token,
                    JobAttemptRecord.lease_expires_at > now,
                )
                .values(lease_expires_at=now + lease_duration, last_heartbeat_at=now)
            )
            if _rowcount(attempt_update) != 1:
                raise _Fenced
            return True

        return self._run_fenced(operation)

    # -- terminalization ---------------------------------------------------------------

    def succeed(
        self,
        claim: ClaimedAttempt,
        *,
        output_text: str,
        finish_reason: str | None,
        usage: ModelUsage,
        elapsed_ms: int,
        now: datetime,
    ) -> bool:
        """Atomically close Attempt + Job + Run as succeeded and append `run.succeeded`."""

        def operation(connection: Connection) -> bool:
            attempt_update = connection.execute(
                update(JobAttemptRecord)
                .where(
                    JobAttemptRecord.id == claim.attempt_id,
                    JobAttemptRecord.job_id == claim.job_id,
                    JobAttemptRecord.status == AttemptStatus.RUNNING.value,
                    JobAttemptRecord.worker_id == claim.worker_id,
                    JobAttemptRecord.claim_token == claim.claim_token,
                    JobAttemptRecord.lease_expires_at > now,
                )
                .values(status=AttemptStatus.SUCCEEDED.value, finished_at=now)
            )
            if _rowcount(attempt_update) != 1:
                raise _Fenced
            job_update = connection.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == claim.job_id,
                    JobRecord.status == JobStatus.RUNNING.value,
                    JobRecord.claimed_by == claim.worker_id,
                    JobRecord.claim_token == claim.claim_token,
                )
                .values(
                    status=JobStatus.SUCCEEDED.value,
                    finished_at=now,
                    updated_at=now,
                    claimed_by=None,
                    claim_token=None,
                    lease_expires_at=None,
                    last_heartbeat_at=None,
                )
            )
            if _rowcount(job_update) != 1:
                raise _Fenced
            run_update = connection.execute(
                update(RunRecord)
                .where(RunRecord.id == claim.run_id, RunRecord.status == RunStatus.RUNNING.value)
                .values(
                    status=RunStatus.SUCCEEDED.value,
                    output_text=output_text,
                    finish_reason=finish_reason,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                    elapsed_ms=elapsed_ms,
                    finished_at=now,
                )
            )
            if _rowcount(run_update) != 1:
                raise _Fenced
            _append_event_on_connection(
                connection,
                run_id=claim.run_id,
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                sequence=_sequence_base(connection, claim.run_id) + 1,
                event_type=RunEventType.RUN_SUCCEEDED,
                attempt_number=claim.attempt_number,
                created_at=now,
            )
            return True

        return self._run_fenced(operation)

    def fail(
        self,
        claim: ClaimedAttempt,
        *,
        error_code: str,
        error_message: str,
        retry_disposition: RetryDisposition,
        usage: ModelUsage,
        elapsed_ms: int,
        now: datetime,
    ) -> bool:
        """Atomically close Attempt + Job + Run as failed and append both failure events."""
        validate_error_message(error_message)

        def operation(connection: Connection) -> bool:
            self._fail_attempt_on_connection(
                connection,
                claim,
                error_code=error_code,
                error_message=error_message,
                retry_disposition=retry_disposition,
                now=now,
            )
            self._close_terminal_on_connection(
                connection,
                claim,
                error_code=error_code,
                error_message=error_message,
                usage=usage,
                elapsed_ms=elapsed_ms,
                now=now,
            )
            return True

        return self._run_fenced(operation)

    def _fail_attempt_on_connection(
        self,
        connection: Connection,
        claim: ClaimedAttempt,
        *,
        error_code: str,
        error_message: str,
        retry_disposition: RetryDisposition,
        now: datetime,
    ) -> None:
        """Close the current Attempt as failed evidence, retaining its full history."""
        attempt_update = connection.execute(
            update(JobAttemptRecord)
            .where(
                JobAttemptRecord.id == claim.attempt_id,
                JobAttemptRecord.job_id == claim.job_id,
                JobAttemptRecord.status == AttemptStatus.RUNNING.value,
                JobAttemptRecord.worker_id == claim.worker_id,
                JobAttemptRecord.claim_token == claim.claim_token,
                JobAttemptRecord.lease_expires_at > now,
            )
            .values(
                status=AttemptStatus.FAILED.value,
                finished_at=now,
                retry_disposition=retry_disposition.value,
                error_code=error_code,
                error_message=error_message,
            )
        )
        if _rowcount(attempt_update) != 1:
            raise _Fenced

    def _close_terminal_on_connection(
        self,
        connection: Connection,
        claim: ClaimedAttempt,
        *,
        error_code: str,
        error_message: str,
        usage: ModelUsage,
        elapsed_ms: int,
        now: datetime,
    ) -> None:
        """Close Job + Run terminally and append both terminal events.

        The Attempt is already closed as failed evidence at this point.
        """
        job_update = connection.execute(
            update(JobRecord)
            .where(
                JobRecord.id == claim.job_id,
                JobRecord.status == JobStatus.RUNNING.value,
                JobRecord.claimed_by == claim.worker_id,
                JobRecord.claim_token == claim.claim_token,
                JobRecord.lease_expires_at > now,
            )
            .values(
                status=JobStatus.FAILED.value,
                finished_at=now,
                updated_at=now,
                error_code=error_code,
                error_message=error_message,
                claimed_by=None,
                claim_token=None,
                lease_expires_at=None,
                last_heartbeat_at=None,
            )
        )
        if _rowcount(job_update) != 1:
            raise _Fenced
        run_update = connection.execute(
            update(RunRecord)
            .where(RunRecord.id == claim.run_id, RunRecord.status == RunStatus.RUNNING.value)
            .values(
                status=RunStatus.FAILED.value,
                finished_at=now,
                elapsed_ms=elapsed_ms,
                error_code=error_code,
                error_message=error_message,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
            )
        )
        if _rowcount(run_update) != 1:
            raise _Fenced
        # Two events in one transaction: read the high-water mark ONCE and assign
        # consecutive sequences. Two independent MAX+1 reads would both claim the same
        # value and fail the unique (run_id, sequence) constraint.
        base = _sequence_base(connection, claim.run_id)
        _append_event_on_connection(
            connection,
            run_id=claim.run_id,
            job_id=claim.job_id,
            attempt_id=claim.attempt_id,
            sequence=base + 1,
            event_type=RunEventType.ATTEMPT_FAILED,
            code=error_code,
            message=error_message,
            attempt_number=claim.attempt_number,
            created_at=now,
        )
        _append_event_on_connection(
            connection,
            run_id=claim.run_id,
            job_id=claim.job_id,
            attempt_id=claim.attempt_id,
            sequence=base + 2,
            event_type=RunEventType.RUN_FAILED,
            code=error_code,
            message=error_message,
            attempt_number=claim.attempt_number,
            created_at=now,
        )

    # -- C4 safe execution retry --------------------------------------------------------

    def record_failure(
        self,
        claim: ClaimedAttempt,
        *,
        error_code: str,
        error_message: str,
        retry_disposition: RetryDisposition,
        usage: ModelUsage,
        elapsed_ms: int,
        anchor_at: datetime,
        retry_policy: RetryPolicy,
        now: datetime,
    ) -> FailureOutcome:
        """Settle one normalized provider failure as a durable retry or an honest terminal.

        This is the only place C4 decides to replay anything, so every precondition is
        revalidated against committed state inside the writing transaction rather than trusted
        from the caller's earlier read: the exact live claim, the exact normalized code *and*
        its disposition, the shared claim budget, and the Run's real pre-existing start.
        """
        validate_error_message(error_message)

        def operation(connection: Connection) -> FailureOutcome:
            return self._failure_on_connection(
                connection,
                claim,
                error_code=error_code,
                error_message=error_message,
                retry_disposition=retry_disposition,
                usage=usage,
                elapsed_ms=elapsed_ms,
                anchor_at=anchor_at,
                retry_policy=retry_policy,
                now=now,
            )

        try:
            return self._runner.run(operation)
        except _Fenced:
            return self._inspect_failure_once(
                claim,
                error_code=error_code,
                retry_disposition=retry_disposition,
                anchor_at=anchor_at,
                retry_policy=retry_policy,
                now=now,
            )
        except (PersistenceContention, PersistenceUnavailable):
            return self._inspect_failure_once(
                claim,
                error_code=error_code,
                retry_disposition=retry_disposition,
                anchor_at=anchor_at,
                retry_policy=retry_policy,
                now=now,
            )

    def _failure_on_connection(
        self,
        connection: Connection,
        claim: ClaimedAttempt,
        *,
        error_code: str,
        error_message: str,
        retry_disposition: RetryDisposition,
        usage: ModelUsage,
        elapsed_ms: int,
        anchor_at: datetime,
        retry_policy: RetryPolicy,
        now: datetime,
    ) -> FailureOutcome:
        # Defense in depth: the disposition alone must never authorize a replay. Only a
        # positively safe, exactly-identified failure may, and unknown codes fail closed to
        # AMBIGUOUS upstream anyway.
        retryable = (
            retry_disposition is RetryDisposition.SAFE_TO_RETRY and error_code == MODEL_RATE_LIMITED
        )
        self._fail_attempt_on_connection(
            connection,
            claim,
            error_code=error_code,
            error_message=error_message,
            retry_disposition=retry_disposition,
            now=now,
        )
        job = (
            connection.execute(
                select(
                    JobRecord.status,
                    JobRecord.claimed_by,
                    JobRecord.claim_token,
                    JobRecord.lease_expires_at,
                    JobRecord.attempt_count,
                    JobRecord.max_attempts,
                    JobRecord.cancel_requested_at,
                ).where(JobRecord.id == claim.job_id)
            )
            .mappings()
            .one_or_none()
        )
        if (
            job is None
            or job["status"] != JobStatus.RUNNING.value
            or job["claimed_by"] != claim.worker_id
            or job["claim_token"] != claim.claim_token
            or job["lease_expires_at"] is None
            or job["lease_expires_at"] <= now
            # A cancellation request revokes permission to continue, so it also revokes
            # permission to schedule a successor Attempt. Like the start fence, this is a
            # redundant guard: the request and the terminal state commit together, so a live
            # claim and a recorded request cannot both be true.
            or job["cancel_requested_at"] is not None
        ):
            raise _Fenced
        budget_remaining = int(job["attempt_count"]) < int(job["max_attempts"])
        # ADR 0017's second layer: an Attempt that dispatched a tool call is never replayed whole.
        # The disposition of the *model* failure cannot license re-running a call whose effect may
        # already have landed, so a dispatched invocation forces the terminal branch regardless of
        # how safe the model failure itself would otherwise have been to replay. `dispatched` is the
        # schema's own definition, not a second one invented here.
        if retryable and self._dispatched_tool_call(connection, claim.attempt_id):
            retryable = False
        if not retryable or not budget_remaining:
            self._close_terminal_on_connection(
                connection,
                claim,
                error_code=error_code,
                error_message=error_message,
                usage=usage,
                elapsed_ms=elapsed_ms,
                now=now,
            )
            return FailureOutcome.TERMINAL_FAILED

        # The ordinal counts *started* execution failures, not committed claims: a Worker that
        # died before the execution-start boundary consumed budget but never called a provider,
        # so letting it inflate the delay would slow a retry for a request that never happened.
        # The Attempt was just closed as `failed` above, so this count already includes it.
        ordinal = self._retry_ordinal_on_connection(connection, claim.job_id)
        due_at = retry_due_at(retry_policy, anchor_at=anchor_at, ordinal=max(ordinal, 1))
        job_update = connection.execute(
            update(JobRecord)
            .where(
                JobRecord.id == claim.job_id,
                JobRecord.status == JobStatus.RUNNING.value,
                JobRecord.claimed_by == claim.worker_id,
                JobRecord.claim_token == claim.claim_token,
                JobRecord.lease_expires_at > now,
            )
            .values(
                status=JobStatus.RETRY_WAIT.value,
                available_at=due_at,
                updated_at=now,
                finished_at=None,
                error_code=None,
                error_message=None,
                claimed_by=None,
                claim_token=None,
                lease_expires_at=None,
                last_heartbeat_at=None,
            )
        )
        if _rowcount(job_update) != 1:
            raise _Fenced
        run = (
            connection.execute(
                select(RunRecord.status, RunRecord.started_at).where(RunRecord.id == claim.run_id)
            )
            .mappings()
            .one_or_none()
        )
        if run is None or run["status"] != RunStatus.RUNNING.value or run["started_at"] is None:
            # A retry may only hang off a Run that genuinely already began executing; the Run
            # itself is deliberately untouched here.
            raise _Fenced
        base = _sequence_base(connection, claim.run_id)
        _append_event_on_connection(
            connection,
            run_id=claim.run_id,
            job_id=claim.job_id,
            attempt_id=claim.attempt_id,
            sequence=base + 1,
            event_type=RunEventType.ATTEMPT_FAILED,
            code=error_code,
            message=error_message,
            attempt_number=claim.attempt_number,
            created_at=now,
        )
        _append_event_on_connection(
            connection,
            run_id=claim.run_id,
            job_id=claim.job_id,
            attempt_id=claim.attempt_id,
            sequence=base + 2,
            event_type=RunEventType.RETRY_SCHEDULED,
            code=error_code,
            message=error_message,
            attempt_number=claim.attempt_number,
            available_at=due_at,
            created_at=now,
        )
        return FailureOutcome.RETRY_SCHEDULED

    def _retry_ordinal_on_connection(self, connection: Connection, job_id: int) -> int:
        """Count this Job's prior started, safely-failed execution Attempts."""
        return int(
            connection.scalar(
                select(func.count())
                .select_from(JobAttemptRecord)
                .where(
                    JobAttemptRecord.job_id == job_id,
                    JobAttemptRecord.status == AttemptStatus.FAILED.value,
                    JobAttemptRecord.retry_disposition == RetryDisposition.SAFE_TO_RETRY.value,
                    JobAttemptRecord.execution_started_at.is_not(None),
                )
            )
            or 0
        )

    def inspect_failure(
        self,
        claim: ClaimedAttempt,
        *,
        error_code: str,
        retry_disposition: RetryDisposition,
        anchor_at: datetime,
        retry_policy: RetryPolicy,
        now: datetime,
    ) -> FailureOutcome:
        """Classify durable state after an unknown failure-settlement outcome."""
        return self._inspect_failure_once(
            claim,
            error_code=error_code,
            retry_disposition=retry_disposition,
            anchor_at=anchor_at,
            retry_policy=retry_policy,
            now=now,
        )

    def _inspect_failure_once(
        self,
        claim: ClaimedAttempt,
        *,
        error_code: str,
        retry_disposition: RetryDisposition,
        anchor_at: datetime,
        retry_policy: RetryPolicy,
        now: datetime,
    ) -> FailureOutcome:
        with self._engine.connect() as connection:
            attempt = (
                connection.execute(
                    select(
                        JobAttemptRecord.status,
                        JobAttemptRecord.retry_disposition,
                        JobAttemptRecord.error_code,
                        JobAttemptRecord.execution_started_at,
                    ).where(JobAttemptRecord.id == claim.attempt_id)
                )
                .mappings()
                .one_or_none()
            )
            job = (
                connection.execute(
                    select(
                        JobRecord.status,
                        JobRecord.claimed_by,
                        JobRecord.claim_token,
                        JobRecord.lease_expires_at,
                        JobRecord.available_at,
                    ).where(JobRecord.id == claim.job_id)
                )
                .mappings()
                .one_or_none()
            )
            run_status = connection.scalar(
                select(RunRecord.status).where(RunRecord.id == claim.run_id)
            )
            retry_event = (
                connection.execute(
                    select(RunEventRecord.available_at).where(
                        RunEventRecord.attempt_id == claim.attempt_id,
                        RunEventRecord.event_type == RunEventType.RETRY_SCHEDULED.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
            ordinal = self._retry_ordinal_on_connection(connection, claim.job_id)

        if attempt is None or job is None or run_status is None:
            return FailureOutcome.UNRESOLVED
        # Checked before any settlement shape: an Attempt still running under this exact live
        # claim means nothing committed, so the durable write — and only the durable write — may
        # be replayed. Reporting this as unresolved would strand an otherwise healthy claim.
        if (
            attempt["status"] == AttemptStatus.RUNNING.value
            and job["status"] == JobStatus.RUNNING.value
            and job["claimed_by"] == claim.worker_id
            and job["claim_token"] == claim.claim_token
            and job["lease_expires_at"] is not None
            and job["lease_expires_at"] > now
        ):
            return FailureOutcome.UNSETTLED
        settled = (
            attempt["status"] == AttemptStatus.FAILED.value
            and attempt["error_code"] == error_code
            and attempt["retry_disposition"] == retry_disposition.value
        )
        if not settled:
            return FailureOutcome.UNRESOLVED
        if (
            job["status"] == JobStatus.RETRY_WAIT.value
            and job["claimed_by"] is None
            and job["claim_token"] is None
            and run_status == RunStatus.RUNNING.value
            and retry_event is not None
        ):
            # The due time must match the one this exact logical operation computes from the
            # same stable anchor and the same durable ordinal. A mismatch means some *other*
            # transition wrote this state, so it is not our commit and must not be adopted.
            expected = retry_due_at(retry_policy, anchor_at=anchor_at, ordinal=max(ordinal, 1))
            if (
                retry_event["available_at"] != job["available_at"]
                or job["available_at"] != expected
            ):
                return FailureOutcome.UNRESOLVED
            return FailureOutcome.RETRY_SCHEDULED
        if (
            job["status"] == JobStatus.FAILED.value
            and run_status == RunStatus.FAILED.value
            and job["claimed_by"] is None
            and job["claim_token"] is None
        ):
            return FailureOutcome.ALREADY_SETTLED
        # A settled Attempt under a still-live claim is not a shape either branch produces, and
        # anything else is equally unexplainable. Fail closed: no replay, and no provider call.
        return FailureOutcome.UNRESOLVED

    def _run_fenced(self, operation: Callable[[Connection], bool]) -> bool:
        try:
            return self._runner.run(operation)
        except _Fenced:
            return False

    @staticmethod
    def _dispatched_tool_call(connection: Connection, attempt_id: int) -> bool:
        """Return whether this Attempt ever crossed a tool call's ambiguity boundary.

        `requested`, `denied` and `cancelled` invocations are deliberately absent: a call that was
        never dispatched provably produced no effect, so it is not a reason to refuse a retry. The
        lookup is served by the existing `(attempt_id, tool_sequence)` unique index as a bounded
        search rather than a scan.
        """
        dispatched = tuple(status.value for status in DISPATCHED_STATUSES)
        return (
            connection.execute(
                select(ToolInvocationRecord.id)
                .where(
                    ToolInvocationRecord.attempt_id == attempt_id,
                    ToolInvocationRecord.status.in_(dispatched),
                )
                .limit(1)
            ).first()
            is not None
        )

    def persist_attempt_usage(
        self, claim: ClaimHandle, *, usage: ModelUsage, now: datetime
    ) -> bool:
        """Durably record the aggregate usage of a multi-turn Attempt.

        ADR 0017 requires a model turn's reported cost to survive a crash that follows it, so this
        is written after every turn and before anything the next turn depends on. It updates only
        the Attempt's own aggregate columns: the Run's usage keeps its Stage C meaning and is still
        written exactly once, at terminalization.
        """
        if any(value is not None and value < 0 for value in usage.values()):
            raise ValueError("usage must be nonnegative")

        def operation(connection: Connection) -> bool:
            # The Job's *current* claim is checked as well as the Attempt's recorded one: a reclaim
            # replaces the former and appends a new Attempt, so a Worker that lost its Job must not
            # be able to keep writing to the Attempt it used to own.
            current = connection.execute(
                select(JobRecord.id)
                .join(JobAttemptRecord, JobAttemptRecord.job_id == JobRecord.id)
                .where(
                    JobAttemptRecord.id == claim.attempt_id,
                    JobRecord.id == claim.job_id,
                    JobRecord.status == JobStatus.RUNNING.value,
                    JobRecord.claimed_by == claim.worker_id,
                    JobRecord.claim_token == claim.claim_token,
                )
            ).first()
            if current is None:
                raise _Fenced
            updated = connection.execute(
                update(JobAttemptRecord)
                .where(
                    JobAttemptRecord.id == claim.attempt_id,
                    JobAttemptRecord.job_id == claim.job_id,
                    JobAttemptRecord.worker_id == claim.worker_id,
                    JobAttemptRecord.claim_token == claim.claim_token,
                    JobAttemptRecord.status == AttemptStatus.RUNNING.value,
                    JobAttemptRecord.lease_expires_at > now,
                )
                .values(
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                )
            )
            if _rowcount(updated) != 1:
                raise _Fenced
            return True

        return self._run_fenced(operation)

    # -- legacy closeout ---------------------------------------------------------------

    def close_legacy_nonterminal_runs(self, *, now: datetime) -> int:
        """Close legacy `running` Runs that have no Job, once, under operator control.

        Only Runs with a real `started_at` and no durable obligation are eligible, so the
        resulting terminal shape uses the Run's own history rather than an invented one. A
        never-started `created` Run has no schema-legal terminal representation and is left
        exactly as it is. No Job, Attempt, or Event is ever synthesized.
        """

        def operation(connection: Connection) -> int:
            candidates = (
                connection.execute(
                    select(RunRecord.id, RunRecord.started_at).where(
                        RunRecord.status == RunStatus.RUNNING.value,
                        RunRecord.id.not_in(select(JobRecord.run_id)),
                    )
                )
                .mappings()
                .all()
            )
            closed = 0
            for candidate in candidates:
                result = connection.execute(
                    update(RunRecord)
                    .where(
                        RunRecord.id == candidate["id"],
                        RunRecord.status == RunStatus.RUNNING.value,
                    )
                    .values(
                        status=RunStatus.FAILED.value,
                        error_code=EXECUTION_OUTCOME_AMBIGUOUS,
                        error_message=safe_error_message(EXECUTION_OUTCOME_AMBIGUOUS),
                        elapsed_ms=_elapsed_ms(candidate["started_at"], now),
                        finished_at=now,
                    )
                )
                closed += _rowcount(result)
            return closed

        return self._runner.run(operation)

    # -- worker registry -----------------------------------------------------------------

    def register_worker(self, *, worker_id: str, now: datetime) -> None:
        """Durably register one process incarnation. A restart draws a new identity."""

        def operation(connection: Connection) -> bool:
            connection.execute(
                insert(WorkerRecord).values(
                    worker_id=worker_id,
                    started_at=now,
                    last_heartbeat_at=now,
                    stopped_at=None,
                )
            )
            return True

        self._runner.run(operation)

    def heartbeat_worker(self, *, worker_id: str, now: datetime) -> WorkerLiveness:
        """Renew registry liveness, monotonically.

        A backwards wall clock must never make a healthy process look older, so the write refuses
        to move `last_heartbeat_at` backwards. That makes a zero-row result ambiguous, which is
        why the row is read back first: `REGRESSED` is benign, while `UNREGISTERED` and `STOPPED`
        mean this incarnation may no longer claim.
        """

        def operation(connection: Connection) -> WorkerLiveness:
            row = (
                connection.execute(
                    select(WorkerRecord.stopped_at, WorkerRecord.last_heartbeat_at).where(
                        WorkerRecord.worker_id == worker_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return WorkerLiveness.UNREGISTERED
            if row["stopped_at"] is not None:
                return WorkerLiveness.STOPPED
            if row["last_heartbeat_at"] >= now:
                return WorkerLiveness.REGRESSED
            connection.execute(
                update(WorkerRecord)
                .where(
                    WorkerRecord.worker_id == worker_id,
                    WorkerRecord.stopped_at.is_(None),
                    WorkerRecord.last_heartbeat_at < now,
                )
                .values(last_heartbeat_at=now)
            )
            return WorkerLiveness.RENEWED

        return self._runner.run(operation)

    def stop_worker(self, *, worker_id: str, now: datetime) -> bool:
        """Record a graceful stop. `stopped_at` mirrors the final heartbeat exactly.

        Mirroring rather than stamping `now` keeps `stopped_at >= last_heartbeat_at` true even if
        the wall clock moved backwards during the process lifetime.
        """

        def operation(connection: Connection) -> bool:
            connection.execute(
                update(WorkerRecord)
                .where(
                    WorkerRecord.worker_id == worker_id,
                    WorkerRecord.stopped_at.is_(None),
                    WorkerRecord.last_heartbeat_at < now,
                )
                .values(last_heartbeat_at=now)
            )
            result = connection.execute(
                update(WorkerRecord)
                .where(
                    WorkerRecord.worker_id == worker_id,
                    WorkerRecord.stopped_at.is_(None),
                )
                .values(stopped_at=WorkerRecord.last_heartbeat_at)
            )
            return _rowcount(result) == 1

        return self._runner.run(operation)

    def list_workers(self, *, now: datetime, stale_after: timedelta) -> list[WorkerSnapshot]:
        """Read-only registry report. Registry health never mutates a Job."""
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(
                        WorkerRecord.worker_id,
                        WorkerRecord.started_at,
                        WorkerRecord.last_heartbeat_at,
                        WorkerRecord.stopped_at,
                    ).order_by(WorkerRecord.id)
                )
                .mappings()
                .all()
            )
        return [
            WorkerSnapshot(
                worker_id=row["worker_id"],
                started_at=row["started_at"],
                last_heartbeat_at=row["last_heartbeat_at"],
                stopped_at=row["stopped_at"],
                state=classify_worker(
                    stopped_at=row["stopped_at"],
                    last_heartbeat_at=row["last_heartbeat_at"],
                    now=now,
                    stale_after=stale_after,
                ),
            )
            for row in rows
        ]

    # -- expired-lease reclamation -------------------------------------------------------

    def reclaim_next_expired_claim(
        self, *, now: datetime, backoff: timedelta
    ) -> ReclaimedClaim | None:
        """Reconcile at most one expired active claim, or return None when none is eligible.

        Authority is the expired Job lease plus the exact current claim tuple. Registry health,
        wall-clock age, and Run status are never consulted. The candidate predicate admits only
        *consistent* Job/Attempt pairings, so a selected row is always actionable and the caller
        can never spin on an unreclaimable one.
        """

        def operation(connection: Connection) -> ReclaimedClaim | None:
            candidate = (
                connection.execute(
                    select(
                        JobRecord.id,
                        JobRecord.run_id,
                        JobRecord.status,
                        JobRecord.claimed_by,
                        JobRecord.claim_token,
                        JobRecord.attempt_count,
                        JobRecord.max_attempts,
                        JobAttemptRecord.id.label("attempt_id"),
                        JobAttemptRecord.attempt_number,
                    )
                    .select_from(JobRecord)
                    .join(JobAttemptRecord, JobAttemptRecord.job_id == JobRecord.id)
                    .where(
                        JobRecord.lease_expires_at.is_not(None),
                        JobRecord.lease_expires_at <= now,
                        JobAttemptRecord.status.in_(
                            (AttemptStatus.CLAIMED.value, AttemptStatus.RUNNING.value)
                        ),
                        or_(
                            and_(
                                JobRecord.status == JobStatus.CLAIMED.value,
                                JobAttemptRecord.status == AttemptStatus.CLAIMED.value,
                                JobAttemptRecord.execution_started_at.is_(None),
                            ),
                            and_(
                                JobRecord.status == JobStatus.RUNNING.value,
                                JobAttemptRecord.status == AttemptStatus.RUNNING.value,
                                JobAttemptRecord.execution_started_at.is_not(None),
                            ),
                        ),
                    )
                    .order_by(JobRecord.lease_expires_at.asc(), JobRecord.id.asc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if candidate is None:
                return None
            if candidate["status"] == JobStatus.CLAIMED.value:
                if candidate["attempt_count"] < candidate["max_attempts"]:
                    return self._reclaim_pre_start_requeue(connection, candidate, now, backoff)
                return self._reclaim_pre_start_exhausted(connection, candidate, now)
            return self._reclaim_post_start_ambiguous(connection, candidate, now)

        try:
            return self._runner.run(operation)
        except _Fenced:
            # A concurrent authority change won the race. Nothing was written.
            return None

    def _expire_pre_start_attempt(
        self, connection: Connection, candidate: RowMapping, now: datetime
    ) -> None:
        """Close an expired pre-start Attempt as evidence, atomically fenced on the old claim."""
        result = connection.execute(
            update(JobAttemptRecord)
            .where(
                JobAttemptRecord.id == candidate["attempt_id"],
                JobAttemptRecord.job_id == candidate["id"],
                JobAttemptRecord.status == AttemptStatus.CLAIMED.value,
                JobAttemptRecord.execution_started_at.is_(None),
                JobAttemptRecord.worker_id == candidate["claimed_by"],
                JobAttemptRecord.claim_token == candidate["claim_token"],
                JobAttemptRecord.lease_expires_at <= now,
            )
            .values(
                status=AttemptStatus.EXPIRED.value,
                finished_at=now,
                retry_disposition=RetryDisposition.SAFE_TO_RETRY.value,
                error_code=None,
                error_message=None,
            )
        )
        if _rowcount(result) != 1:
            raise _Fenced

    def _clear_claim_values(self, now: datetime) -> dict[str, Any]:
        return {
            "updated_at": now,
            "claimed_by": None,
            "claim_token": None,
            "lease_expires_at": None,
            "last_heartbeat_at": None,
        }

    def _reclaim_pre_start_requeue(
        self,
        connection: Connection,
        candidate: RowMapping,
        now: datetime,
        backoff: timedelta,
    ) -> ReclaimedClaim:
        """Case A: the boundary never committed and another claim is available."""
        self._expire_pre_start_attempt(connection, candidate, now)
        values = self._clear_claim_values(now)
        values.update(
            status=JobStatus.QUEUED.value,
            available_at=now + backoff,
            finished_at=None,
            error_code=None,
            error_message=None,
        )
        result = connection.execute(
            update(JobRecord)
            .where(
                JobRecord.id == candidate["id"],
                JobRecord.status == JobStatus.CLAIMED.value,
                JobRecord.claimed_by == candidate["claimed_by"],
                JobRecord.claim_token == candidate["claim_token"],
                JobRecord.lease_expires_at <= now,
            )
            .values(**values)
        )
        if _rowcount(result) != 1:
            raise _Fenced
        base = _sequence_base(connection, int(candidate["run_id"]))
        self._append_recovery_events(
            connection, candidate, now, base, RunEventType.RECOVERY_PRE_START
        )
        return self._reclaimed(ReclamationKind.PRE_START_REQUEUED, candidate)

    def _reclaim_pre_start_exhausted(
        self, connection: Connection, candidate: RowMapping, now: datetime
    ) -> ReclaimedClaim:
        """Case B: the boundary never committed and the claim budget is spent.

        The Run never started, so it is closed as `failed` with no `started_at` and no
        `elapsed_ms`. Fabricating either would assert an execution window that never existed.
        """
        self._expire_pre_start_attempt(connection, candidate, now)
        message = safe_error_message(WORKER_RECOVERY_EXHAUSTED)
        values = self._clear_claim_values(now)
        values.update(
            status=JobStatus.FAILED.value,
            finished_at=now,
            error_code=WORKER_RECOVERY_EXHAUSTED,
            error_message=message,
        )
        job_update = connection.execute(
            update(JobRecord)
            .where(
                JobRecord.id == candidate["id"],
                JobRecord.status == JobStatus.CLAIMED.value,
                JobRecord.claimed_by == candidate["claimed_by"],
                JobRecord.claim_token == candidate["claim_token"],
                JobRecord.lease_expires_at <= now,
            )
            .values(**values)
        )
        if _rowcount(job_update) != 1:
            raise _Fenced
        run_update = connection.execute(
            update(RunRecord)
            .where(
                RunRecord.id == candidate["run_id"],
                RunRecord.status == RunStatus.CREATED.value,
                RunRecord.started_at.is_(None),
            )
            .values(
                status=RunStatus.FAILED.value,
                finished_at=now,
                error_code=WORKER_RECOVERY_EXHAUSTED,
                error_message=message,
            )
        )
        if _rowcount(run_update) != 1:
            raise _Fenced
        base = _sequence_base(connection, int(candidate["run_id"]))
        self._append_recovery_events(
            connection, candidate, now, base, RunEventType.RECOVERY_PRE_START
        )
        _append_event_on_connection(
            connection,
            run_id=int(candidate["run_id"]),
            job_id=int(candidate["id"]),
            attempt_id=int(candidate["attempt_id"]),
            sequence=base + 3,
            event_type=RunEventType.RUN_FAILED,
            code=WORKER_RECOVERY_EXHAUSTED,
            message=message,
            attempt_number=int(candidate["attempt_number"]),
            created_at=now,
        )
        return self._reclaimed(ReclamationKind.PRE_START_EXHAUSTED, candidate)

    def _reclaim_post_start_ambiguous(
        self, connection: Connection, candidate: RowMapping, now: datetime
    ) -> ReclaimedClaim:
        """The start boundary committed, so a provider call may have been issued: never replay."""
        result = connection.execute(
            update(JobAttemptRecord)
            .where(
                JobAttemptRecord.id == candidate["attempt_id"],
                JobAttemptRecord.job_id == candidate["id"],
                JobAttemptRecord.status == AttemptStatus.RUNNING.value,
                JobAttemptRecord.execution_started_at.is_not(None),
                JobAttemptRecord.worker_id == candidate["claimed_by"],
                JobAttemptRecord.claim_token == candidate["claim_token"],
                JobAttemptRecord.lease_expires_at <= now,
            )
            .values(
                status=AttemptStatus.EXPIRED.value,
                finished_at=now,
                retry_disposition=RetryDisposition.AMBIGUOUS.value,
                error_code=EXECUTION_OUTCOME_AMBIGUOUS,
                error_message=safe_error_message(EXECUTION_OUTCOME_AMBIGUOUS),
            )
        )
        if _rowcount(result) != 1:
            raise _Fenced
        started_at = connection.scalar(
            select(RunRecord.started_at).where(RunRecord.id == candidate["run_id"])
        )
        message = safe_error_message(EXECUTION_OUTCOME_AMBIGUOUS)
        values = self._clear_claim_values(now)
        values.update(
            status=JobStatus.FAILED.value,
            finished_at=now,
            error_code=EXECUTION_OUTCOME_AMBIGUOUS,
            error_message=message,
        )
        job_update = connection.execute(
            update(JobRecord)
            .where(
                JobRecord.id == candidate["id"],
                JobRecord.status == JobStatus.RUNNING.value,
                JobRecord.claimed_by == candidate["claimed_by"],
                JobRecord.claim_token == candidate["claim_token"],
                JobRecord.lease_expires_at <= now,
            )
            .values(**values)
        )
        if _rowcount(job_update) != 1:
            raise _Fenced
        run_update = connection.execute(
            update(RunRecord)
            .where(
                RunRecord.id == candidate["run_id"],
                RunRecord.status == RunStatus.RUNNING.value,
                RunRecord.started_at.is_not(None),
            )
            .values(
                status=RunStatus.FAILED.value,
                finished_at=now,
                elapsed_ms=_elapsed_ms(started_at, now),
                error_code=EXECUTION_OUTCOME_AMBIGUOUS,
                error_message=message,
            )
        )
        if _rowcount(run_update) != 1:
            raise _Fenced
        base = _sequence_base(connection, int(candidate["run_id"]))
        self._append_recovery_events(
            connection, candidate, now, base, RunEventType.RECOVERY_AMBIGUOUS
        )
        _append_event_on_connection(
            connection,
            run_id=int(candidate["run_id"]),
            job_id=int(candidate["id"]),
            attempt_id=int(candidate["attempt_id"]),
            sequence=base + 3,
            event_type=RunEventType.RUN_FAILED,
            code=EXECUTION_OUTCOME_AMBIGUOUS,
            message=message,
            attempt_number=int(candidate["attempt_number"]),
            created_at=now,
        )
        return self._reclaimed(ReclamationKind.POST_START_AMBIGUOUS, candidate)

    def _append_recovery_events(
        self,
        connection: Connection,
        candidate: RowMapping,
        now: datetime,
        base: int,
        event_type: RunEventType,
    ) -> None:
        """Append `attempt.expired` and the classification event from ONE high-water read.

        The high-water mark is read exactly once by the caller: two independent `MAX(sequence)+1`
        reads would allocate the same value and fail on the per-Run sequence unique constraint.
        """
        number = int(candidate["attempt_number"])
        _append_event_on_connection(
            connection,
            run_id=int(candidate["run_id"]),
            job_id=int(candidate["id"]),
            attempt_id=int(candidate["attempt_id"]),
            sequence=base + 1,
            event_type=RunEventType.ATTEMPT_EXPIRED,
            attempt_number=number,
            created_at=now,
        )
        _append_event_on_connection(
            connection,
            run_id=int(candidate["run_id"]),
            job_id=int(candidate["id"]),
            attempt_id=int(candidate["attempt_id"]),
            sequence=base + 2,
            event_type=event_type,
            attempt_number=number,
            created_at=now,
        )

    def _reclaimed(self, kind: ReclamationKind, candidate: RowMapping) -> ReclaimedClaim:
        return ReclaimedClaim(
            kind=kind,
            run_id=int(candidate["run_id"]),
            job_id=int(candidate["id"]),
            attempt_id=int(candidate["attempt_id"]),
            attempt_number=int(candidate["attempt_number"]),
        )


__all__ = [
    "DurableSubmissionRejected",
    "SqlAlchemyJobExecutionPersistence",
    "SqlAlchemyJobPersistence",
    "run_event_from_record",
    "run_from_record",
]


class SqlAlchemyRunCancellationPersistence:
    """Control-plane-only owner cancellation, with no execution capability at all.

    Cancellation belongs to the control plane: it is a user revoking authority over their own
    Run, exactly as submission is a user creating one. It deliberately lives in its own class
    rather than on the execution-plane store so the API can compose this one seam and gain no
    ability to claim, start, heartbeat, terminalize, or reclaim anything.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._engine = engine
        self._runner = _TransactionRunner(engine, sleep)

    # -- owner cancellation ------------------------------------------------------------

    def cancel_run(self, *, user_id: int, run_id: int, now: datetime) -> CancellationOutcome:
        """Durably cancel one owned Run in a single authoritative transaction.

        Cancellation is authoritative rather than a request. The transition commits inside this
        call, so it completes whether or not a Worker exists, whether or not one is alive, and
        whether the Job is `queued`, `retry_wait`, `claimed`, or `running`. The owning Worker
        discovers the revoked authority through its next heartbeat and stops its local provider
        task; it is never a prerequisite.

        Nothing else in the engine needs to know cancellation exists. A late success, failure,
        retry schedule, start, or heartbeat is already fenced on the live Job claim and the
        `running` Run this transaction destroys, so each one matches zero rows on its own.

        Ownership is re-verified here, in the same transaction as the write, because a check
        performed in a separate read could race the transition it authorizes.
        """

        def operation(connection: Connection) -> CancellationOutcome:
            row = (
                connection.execute(
                    select(RunRecord.status, RunRecord.started_at)
                    .join(
                        AgentInstanceRecord,
                        AgentInstanceRecord.id == RunRecord.agent_instance_id,
                    )
                    .where(
                        RunRecord.id == run_id,
                        AgentInstanceRecord.owner_user_id == user_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return CancellationOutcome.NOT_FOUND
            status = row["status"]
            if status == RunStatus.CANCELLED.value:
                # Idempotent repeat: the earliest request, the original finish instant, and the
                # original elapsed interval all stand, and no second event is appended.
                return CancellationOutcome.CANCELLED
            if status not in (RunStatus.CREATED.value, RunStatus.RUNNING.value):
                return CancellationOutcome.NOT_CANCELLABLE
            job = (
                connection.execute(
                    select(JobRecord.id, JobRecord.status).where(JobRecord.run_id == run_id)
                )
                .mappings()
                .one_or_none()
            )
            if job is None:
                # A nonterminal Run with no durable obligation predates the queue: it has no Job
                # whose authority could be revoked and no legal way to carry a cancellation
                # event, because `run_events.job_id` is NOT NULL. The operator closeout owns
                # that artifact; C5 does not invent a timeline for it.
                return CancellationOutcome.NOT_CANCELLABLE
            job_id = int(job["id"])
            # Write-once. The first accepted request is the durable audit fact; a later request
            # never moves it, and nothing clears it, including terminalization.
            connection.execute(
                update(JobRecord)
                .where(JobRecord.id == job_id, JobRecord.cancel_requested_at.is_(None))
                .values(cancel_requested_at=now)
            )
            active = (
                connection.execute(
                    select(JobAttemptRecord.id, JobAttemptRecord.attempt_number)
                    .where(
                        JobAttemptRecord.job_id == job_id,
                        JobAttemptRecord.status.in_(
                            (AttemptStatus.CLAIMED.value, AttemptStatus.RUNNING.value)
                        ),
                    )
                    .order_by(JobAttemptRecord.attempt_number.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            attempt_id: int | None = None
            attempt_number: int | None = None
            if active is not None:
                attempt_id = int(active["id"])
                attempt_number = int(active["attempt_number"])
                # The Attempt keeps its execution start, its owner, and its token as immutable
                # historical evidence; only its lifecycle closes. A never-started claim keeps a
                # NULL execution_started_at, so a pre-start cancellation can never claim a start
                # boundary it did not have.
                closed_attempt = connection.execute(
                    update(JobAttemptRecord)
                    .where(
                        JobAttemptRecord.id == attempt_id,
                        JobAttemptRecord.job_id == job_id,
                        JobAttemptRecord.status.in_(
                            (AttemptStatus.CLAIMED.value, AttemptStatus.RUNNING.value)
                        ),
                    )
                    .values(status=AttemptStatus.CANCELLED.value, finished_at=now)
                )
                if _rowcount(closed_attempt) != 1:
                    raise _Fenced
            # Authority is released here and nowhere else: the Job stays claimless and
            # errorless of any live owner, while the Attempt keeps its own evidence.
            values: dict[str, Any] = {
                "updated_at": now,
                "claimed_by": None,
                "claim_token": None,
                "lease_expires_at": None,
                "last_heartbeat_at": None,
            }
            values.update(
                status=JobStatus.CANCELLED.value,
                finished_at=now,
                error_code=EXECUTION_CANCELLED,
                error_message=safe_error_message(EXECUTION_CANCELLED),
            )
            closed_job = connection.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == job_id,
                    # Pinned on the observed source status rather than a broad set, so a Job
                    # that moved for any other reason cannot be rewritten by this transition.
                    JobRecord.status == job["status"],
                )
                .values(**values)
            )
            if _rowcount(closed_job) != 1:
                raise _Fenced
            started_at = row["started_at"]
            closed_run = connection.execute(
                update(RunRecord)
                .where(RunRecord.id == run_id, RunRecord.status == status)
                .values(
                    status=RunStatus.CANCELLED.value,
                    finished_at=now,
                    # Truthful only: a Run that never started has no duration to report, and a
                    # Run that did start reports the real interval until cancellation was
                    # accepted -- the same semantics C3 uses for a post-start recovery close.
                    # It is deliberately not the provider-completion duration.
                    elapsed_ms=None if started_at is None else _elapsed_ms(started_at, now),
                )
            )
            if _rowcount(closed_run) != 1:
                raise _Fenced
            base = _sequence_base(connection, run_id)
            for offset, event_type in enumerate(
                (RunEventType.CANCELLATION_REQUESTED, RunEventType.RUN_CANCELLED), start=1
            ):
                _append_event_on_connection(
                    connection,
                    run_id=run_id,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    sequence=base + offset,
                    event_type=event_type,
                    code=EXECUTION_CANCELLED,
                    message=safe_error_message(EXECUTION_CANCELLED),
                    attempt_number=attempt_number,
                    created_at=now,
                )
            return CancellationOutcome.CANCELLED

        return self._runner.run(operation)
