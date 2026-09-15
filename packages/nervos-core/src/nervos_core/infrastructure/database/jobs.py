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

from sqlalchemy import Connection, Engine, and_, func, insert, or_, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from nervos_core.application.agents import DurableSubmissionRejected
from nervos_core.application.errors import (
    PersistenceContention,
    PersistenceUnavailable,
    QueueCapacityExceeded,
)
from nervos_core.application.job_execution import ClaimedAttempt, ClaimState
from nervos_core.application.lease_reclamation import (
    ReclaimedClaim,
    ReclamationKind,
    WorkerLiveness,
    WorkerSnapshot,
    classify_worker,
)
from nervos_core.application.model_completion import (
    EXECUTION_OUTCOME_AMBIGUOUS,
    safe_error_message,
)
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.jobs import AttemptStatus, JobStatus, RetryDisposition, RunEventType
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
    JobAttemptRecord,
    JobRecord,
    RunEventRecord,
    RunRecord,
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

# Job states that occupy durable queue capacity. `retry_wait` is included even though C2
# never writes it, so a later milestone cannot silently raise the ceiling.
_OCCUPYING_STATUSES = (
    JobStatus.QUEUED.value,
    JobStatus.CLAIMED.value,
    JobStatus.RUNNING.value,
    JobStatus.RETRY_WAIT.value,
)

_T = TypeVar("_T")


class _Fenced(RuntimeError):
    """A fenced update affected zero rows, so the whole transaction must roll back."""


def run_from_record(record: RunRecord) -> Run:
    """Map one persisted Run row onto the immutable domain value."""
    limits = RunLimits(
        record.input_max_bytes,
        record.input_max_code_points,
        record.output_max_bytes,
        record.output_max_code_points,
        record.provider_timeout_ms,
        record.max_output_tokens,
        record.max_model_calls,
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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 1 <= max_pending <= 100_000:
            raise ValueError("max_pending must be between 1 and 100000")
        self._engine = engine
        self._max_pending = max_pending
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
    ) -> Run:
        """Atomically snapshot an owned enabled Instance into Run, Job, and two events."""
        validate_input_text(input_text, limits)
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        capacity = self._max_pending if max_pending is None else max_pending
        if not 1 <= capacity <= 100_000:
            raise ValueError("max_pending must be between 1 and 100000")

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

        # The hard pending cap is counted inside the same `BEGIN IMMEDIATE` transaction that
        # inserts the Job, so two concurrent submissions serialize on the write lock: the
        # second reads the first's committed Job rather than racing it.
        pending = int(
            connection.scalar(
                select(func.count())
                .select_from(JobRecord)
                .where(JobRecord.status.in_(_OCCUPYING_STATUSES))
            )
            or 0
        )
        if pending >= capacity:
            raise QueueCapacityExceeded

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
        )

    def _reconcile_submission(self, run_id: int) -> Run | None:
        """Read back an unobserved submission outcome instead of blindly replaying it.

        The Run identifier is known because the insert had already executed when the failure
        happened, so this is an exact read rather than a guess. A Run that is present and
        complete is treated as committed; a Run with missing children is an invariant
        failure and is never replayed; an absent Run means nothing committed.
        """
        with self._engine.connect() as connection:
            job_id = connection.scalar(
                select(func.min(JobRecord.id)).where(JobRecord.run_id == run_id)
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
        if job_id is None or sequences != [
            (1, RunEventType.RUN_CREATED.value),
            (2, RunEventType.RUN_QUEUED.value),
        ]:
            raise PersistenceUnavailable
        return run_from_record(record)

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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._engine = engine
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
        """Claim the oldest eligible Job, or return None.

        Eligibility is capability-aware: a Worker only ever claims a Job whose provider it
        has configured. An empty configured set issues no query at all rather than a
        malformed empty `IN` list.
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
        # Only live leases occupy active capacity. That is coherent precisely because an
        # expired lease means no write authority, so freeing the slot cannot produce two
        # authorized concurrent executions.
        active = int(
            connection.scalar(
                select(func.count())
                .select_from(JobRecord)
                .where(
                    JobRecord.status.in_((JobStatus.CLAIMED.value, JobStatus.RUNNING.value)),
                    JobRecord.lease_expires_at > now,
                )
            )
            or 0
        )
        if active >= max_active:
            return None

        candidate = (
            connection.execute(
                select(
                    JobRecord.id,
                    JobRecord.run_id,
                    JobRecord.model_provider,
                    JobRecord.attempt_count,
                    JobRecord.max_attempts,
                )
                .where(
                    JobRecord.status == JobStatus.QUEUED.value,
                    JobRecord.available_at <= now,
                    JobRecord.attempt_count < JobRecord.max_attempts,
                    JobRecord.model_provider.in_(provider_ids),
                )
                .order_by(JobRecord.available_at.asc(), JobRecord.id.asc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if candidate is None:
            return None

        job_id = int(candidate["id"])
        run_id = int(candidate["run_id"])
        attempt_number = int(candidate["attempt_count"]) + 1
        progress.job_id = job_id
        progress.run_id = run_id
        progress.token = token
        progress.lease_expires_at = lease_expires_at

        updated = connection.execute(
            update(JobRecord)
            .where(
                JobRecord.id == job_id,
                JobRecord.status == JobStatus.QUEUED.value,
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
        if job["status"] in (
            JobStatus.SUCCEEDED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        ) or attempt_status in (
            AttemptStatus.SUCCEEDED.value,
            AttemptStatus.FAILED.value,
            AttemptStatus.CANCELLED.value,
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
        """Commit the execution-start boundary before any external call is made."""

        def operation(connection: Connection) -> bool:
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
                )
                .values(status=JobStatus.RUNNING.value, updated_at=now)
            )
            if _rowcount(job_update) != 1:
                raise _Fenced
            run_update = connection.execute(
                update(RunRecord)
                .where(
                    RunRecord.id == claim.run_id,
                    RunRecord.status == RunStatus.CREATED.value,
                )
                .values(status=RunStatus.RUNNING.value, started_at=now)
            )
            if _rowcount(run_update) != 1:
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

        return self._run_fenced(operation)

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
            job_update = connection.execute(
                update(JobRecord)
                .where(
                    JobRecord.id == claim.job_id,
                    JobRecord.status == JobStatus.RUNNING.value,
                    JobRecord.claimed_by == claim.worker_id,
                    JobRecord.claim_token == claim.claim_token,
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
            return True

        return self._run_fenced(operation)

    def _run_fenced(self, operation: Callable[[Connection], bool]) -> bool:
        try:
            return self._runner.run(operation)
        except _Fenced:
            return False

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
    "run_from_record",
]
