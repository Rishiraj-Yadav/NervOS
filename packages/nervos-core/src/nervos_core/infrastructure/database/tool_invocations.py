"""Durable Tool Invocation state over the accepted D1 schema.

One short `BEGIN IMMEDIATE` transaction per transition, and nothing else. The module exists so the
tool loop's durable writes have a single home separate from both the grant authority (`tools.py`)
and the Job/Attempt engine (`jobs.py`): a tool invocation is its own durable record with its own
lifecycle, and blurring it into either neighbour would make the module whose job is to be
authoritative about *grants* also authoritative about *calls*.

**Every write is fenced.** A transition is refused unless the Job, Attempt, Worker identity, claim
token, lease, and cancellation state all still agree, so a Worker that lost authority cannot make a
late write look current. A refused write mutates nothing at all.

**`record_requested` is where the start boundary is enforced.** The rule that no tool call may be
recorded before `execution_started_at` is committed is a property of this insert, not a convention
its callers are trusted to follow.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import Engine, insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.application.model_completion import TOOL_DENIED, safe_error_message
from nervos_core.application.tool_invocations import (
    ALLOWED_DECISION,
    ClaimHandle,
    InvocationRequest,
    InvocationStatus,
    RecordOutcome,
    RecordOutcomeKind,
    RefusalOutcome,
    RefusalOutcomeKind,
    ResultEnvelope,
    StartOutcome,
    StartOutcomeKind,
    arguments_digest,
    arguments_shape,
    permission_decision_value,
)
from nervos_core.application.tool_permissions import PermissionDecision
from nervos_core.domain.jobs import AttemptStatus, JobStatus, RunEventType
from nervos_core.infrastructure.database.models import (
    JobAttemptRecord,
    JobRecord,
    ToolInvocationRecord,
)
from nervos_core.infrastructure.database.run_events import (
    EventOwnershipViolation,
    append_event_on_connection,
    sequence_base,
)
from nervos_core.infrastructure.database.tools import evaluate_permission_on_connection

_T = TypeVar("_T")
_TRANSACTION_ATTEMPTS = 5


class _Fenced(Exception):
    """A compare-and-set found its row in an unexpected state, so nothing was written."""


@dataclass(frozen=True, slots=True)
class _Authority:
    """What a claim may still do: write at all, and claim a call was dispatched."""

    live: bool
    cancelled: bool


def _is_contention(error: SQLAlchemyError) -> bool:
    """True if the error is provably a busy/locked failure rather than any other database error."""
    message = str(error).lower()
    return "locked" in message or "busy" in message


class _TransactionRunner:
    """One short BEGIN IMMEDIATE transaction per operation, retried only on proven contention."""

    def __init__(self, engine: Engine, sleep: Callable[[float], None] = time.sleep) -> None:
        self._engine = engine
        self._sleep = sleep

    def run(self, operation: Callable[[Connection], _T]) -> _T:
        last_error: SQLAlchemyError | None = None
        for index in range(_TRANSACTION_ATTEMPTS):
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
                connection.close()

            if index < _TRANSACTION_ATTEMPTS - 1:
                self._sleep(0.010 * (2**index))

        raise PersistenceUnavailable from last_error


class SqlAlchemyToolInvocationPersistence:
    """Fenced durable writes for one tool invocation's lifecycle."""

    def __init__(self, engine: Engine) -> None:
        self._runner = _TransactionRunner(engine)

    def record_requested(
        self, *, claim: ClaimHandle, request: InvocationRequest, now: datetime
    ) -> RecordOutcome:
        """Persist the intent to call a tool, or refuse and insert nothing.

        The predicate below states the start boundary as a durable invariant: the parent Attempt
        must already carry `execution_started_at`, and the claim must still be live and uncancelled.
        A violation is not "a row we did not expect" -- it is a Worker acting on authority it no
        longer has, so nothing is written and the caller is told authority was lost.
        """
        arguments = dict(request.arguments)
        digest = arguments_digest(arguments)
        shape = arguments_shape(arguments)
        decision = permission_decision_value(request.permission_decision)

        def operation(connection: Connection) -> RecordOutcome:
            if not self._authorized(connection, claim=claim, now=now):
                return RecordOutcome(RecordOutcomeKind.FENCED)
            # The requested triple must be the claim's own. The fence above validates the *claim*
            # against durable state, so writing a different Job/Run/Attempt from the request would
            # record a row the fence never checked -- exactly the authority this method exists to
            # make unrepresentable. A mismatch is a caller defect, so nothing is inserted.
            if (
                request.run_id != claim.run_id
                or request.job_id != claim.job_id
                or request.attempt_id != claim.attempt_id
            ):
                return RecordOutcome(RecordOutcomeKind.FENCED)
            result = connection.execute(
                insert(ToolInvocationRecord).values(
                    run_id=request.run_id,
                    job_id=request.job_id,
                    attempt_id=request.attempt_id,
                    tool_sequence=request.tool_sequence,
                    tool_definition_id=request.tool_definition_id,
                    source_kind=request.source_kind.value,
                    source_id=request.source_id,
                    upstream_name=request.upstream_name,
                    model_name=request.model_name,
                    definition_fingerprint=request.definition_fingerprint,
                    status=InvocationStatus.REQUESTED.value,
                    permission_decision=decision,
                    provider_call_id=request.provider_call_id,
                    requested_at=now,
                    arguments_digest=digest,
                    arguments_shape=shape,
                )
            )
            primary_key = result.inserted_primary_key
            if primary_key is None or primary_key[0] is None:  # pragma: no cover - defensive
                raise _Fenced
            invocation_id = int(primary_key[0])
            # The event is appended in the same transaction as the row it describes, so the public
            # timeline can never show a request whose durable invocation rolled back, nor an
            # invocation with no request on the timeline.
            self._append_tool_event(
                connection,
                claim=claim,
                event_type=RunEventType.TOOL_REQUESTED,
                tool_invocation_id=invocation_id,
                now=now,
            )
            return RecordOutcome(RecordOutcomeKind.REQUESTED, invocation_id)

        return self._runner.run(operation)

    def mark_started(
        self, *, claim: ClaimHandle, invocation_id: int, now: datetime
    ) -> StartOutcome:
        """Re-check authority and the live permission predicate, then commit exactly one branch.

        This is the ambiguity boundary. Before it commits, the call provably did not happen; after
        it commits, it may have. All three branches are decided inside one transaction against a row
        still in `requested`, so no interleaving can produce a started call that a concurrent
        revocation had already forbidden.
        """

        def operation(connection: Connection) -> StartOutcome:
            authority = self._authority(connection, claim=claim, now=now)
            if not authority.live:
                return StartOutcome(StartOutcomeKind.FENCED)
            if authority.cancelled:
                # Cancellation before the start boundary is a refusal, not ambiguity: the call
                # provably did not run, so it is closed as cancelled with no start recorded.
                if (
                    _rowcount(
                        self._close_non_dispatched(
                            connection,
                            invocation_id,
                            attempt_id=claim.attempt_id,
                            status=InvocationStatus.CANCELLED,
                            decision=None,
                            now=now,
                        )
                    )
                    != 1
                ):
                    raise _Fenced
                return StartOutcome(StartOutcomeKind.CANCELLED, invocation_id)

            row = (
                connection.execute(
                    select(
                        ToolInvocationRecord.status,
                        ToolInvocationRecord.run_id,
                        ToolInvocationRecord.tool_definition_id,
                    ).where(
                        ToolInvocationRecord.id == invocation_id,
                        ToolInvocationRecord.attempt_id == claim.attempt_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["status"] != InvocationStatus.REQUESTED.value:
                return StartOutcome(StartOutcomeKind.FENCED)

            decision = evaluate_permission_on_connection(
                connection,
                run_id=int(row["run_id"]),
                tool_definition_id=int(row["tool_definition_id"]),
            )
            if not decision.allowed:
                if (
                    _rowcount(
                        self._close_non_dispatched(
                            connection,
                            invocation_id,
                            attempt_id=claim.attempt_id,
                            status=InvocationStatus.DENIED,
                            decision=decision,
                            now=now,
                        )
                    )
                    != 1
                ):
                    raise _Fenced
                # This branch is the *live* re-check, and it is the one that catches a grant
                # revoked, a connection disabled, a definition drifted, or an owner changed
                # between the two checks. It must emit its own denial event here: this call never
                # reaches `mark_denied`, so a missing append would leave a durable `denied` row
                # with no `tool.denied` on the timeline -- the exact gap this milestone closes.
                self._append_tool_event(
                    connection,
                    claim=claim,
                    event_type=RunEventType.TOOL_DENIED,
                    tool_invocation_id=invocation_id,
                    code=TOOL_DENIED,
                    message=safe_error_message(TOOL_DENIED),
                    now=now,
                )
                return StartOutcome(
                    StartOutcomeKind.DENIED, invocation_id, permission_decision_value(decision)
                )

            updated = connection.execute(
                update(ToolInvocationRecord)
                .where(
                    ToolInvocationRecord.id == invocation_id,
                    ToolInvocationRecord.attempt_id == claim.attempt_id,
                    ToolInvocationRecord.status == InvocationStatus.REQUESTED.value,
                )
                .values(status=InvocationStatus.STARTED.value, started_at=now)
            )
            if _rowcount(updated) != 1:
                raise _Fenced
            # The start boundary is the one transition whose event matters most: it is the moment
            # the call may already have reached an external system, so the timeline must show it
            # before any terminal fact can exist.
            self._append_tool_event(
                connection,
                claim=claim,
                event_type=RunEventType.TOOL_STARTED,
                tool_invocation_id=invocation_id,
                now=now,
            )
            return StartOutcome(StartOutcomeKind.STARTED, invocation_id, ALLOWED_DECISION)

        try:
            return self._runner.run(operation)
        except (_Fenced, EventOwnershipViolation):
            return StartOutcome(StartOutcomeKind.FENCED)

    def mark_denied(
        self,
        *,
        claim: ClaimHandle,
        invocation_id: int,
        decision: PermissionDecision,
        now: datetime,
    ) -> bool:
        """Close a requested call as denied, before the start boundary was ever crossed."""

        def operation(connection: Connection) -> bool:
            if not self._authorized(connection, claim=claim, now=now):
                return False
            updated = self._close_non_dispatched(
                connection,
                invocation_id,
                attempt_id=claim.attempt_id,
                status=InvocationStatus.DENIED,
                decision=decision,
                now=now,
            )
            if _rowcount(updated) != 1:
                raise _Fenced
            self._append_tool_event(
                connection,
                claim=claim,
                event_type=RunEventType.TOOL_DENIED,
                tool_invocation_id=invocation_id,
                code=TOOL_DENIED,
                message=safe_error_message(TOOL_DENIED),
                now=now,
            )
            return True

        return self._fenced(operation)

    def mark_cancelled(self, *, claim: ClaimHandle, invocation_id: int, now: datetime) -> bool:
        """Close a requested call as cancelled, before the start boundary was ever crossed.

        Cancellation is not an outcome the tool had, so the permission decision recorded at request
        time is left exactly as it was and no result metadata is written.
        """

        def operation(connection: Connection) -> bool:
            if not self._authorized(connection, claim=claim, now=now):
                return False
            updated = self._close_non_dispatched(
                connection,
                invocation_id,
                attempt_id=claim.attempt_id,
                status=InvocationStatus.CANCELLED,
                decision=None,
                now=now,
            )
            if _rowcount(updated) != 1:
                raise _Fenced
            return True

        return self._fenced(operation)

    def mark_succeeded(
        self,
        *,
        claim: ClaimHandle,
        invocation_id: int,
        envelope: ResultEnvelope,
        now: datetime,
    ) -> bool:
        """Close a started call as succeeded, recording its content evidence."""

        def operation(connection: Connection) -> bool:
            if not self._conclude(connection, claim=claim, now=now):
                return False
            updated = connection.execute(
                update(ToolInvocationRecord)
                .where(
                    ToolInvocationRecord.id == invocation_id,
                    ToolInvocationRecord.attempt_id == claim.attempt_id,
                    ToolInvocationRecord.status == InvocationStatus.STARTED.value,
                )
                .values(
                    status=InvocationStatus.SUCCEEDED.value,
                    finished_at=now,
                    result_digest=envelope.digest,
                    result_bytes=envelope.bytes,
                    # The envelope is the full untruncated evidence, so nothing was withheld from
                    # the durable record even when the model-visible observation was shortened.
                    result_truncated=False,
                )
            )
            if _rowcount(updated) != 1:
                raise _Fenced
            self._append_tool_event(
                connection,
                claim=claim,
                event_type=RunEventType.TOOL_SUCCEEDED,
                tool_invocation_id=invocation_id,
                now=now,
            )
            return True

        return self._fenced(operation)

    def mark_failed(
        self,
        *,
        claim: ClaimHandle,
        invocation_id: int,
        error_code: str,
        error_message: str,
        now: datetime,
    ) -> bool:
        """Close a started call as failed, with an already-classified safe error pair."""

        def operation(connection: Connection) -> bool:
            if not self._conclude(connection, claim=claim, now=now):
                return False
            updated = connection.execute(
                update(ToolInvocationRecord)
                .where(
                    ToolInvocationRecord.id == invocation_id,
                    ToolInvocationRecord.attempt_id == claim.attempt_id,
                    ToolInvocationRecord.status == InvocationStatus.STARTED.value,
                )
                .values(
                    status=InvocationStatus.FAILED.value,
                    finished_at=now,
                    error_code=error_code,
                    error_message=error_message,
                )
            )
            if _rowcount(updated) != 1:
                raise _Fenced
            self._append_tool_event(
                connection,
                claim=claim,
                event_type=RunEventType.TOOL_FAILED,
                tool_invocation_id=invocation_id,
                code=error_code,
                message=error_message,
                now=now,
            )
            return True

        return self._fenced(operation)

    def mark_ambiguous(
        self,
        *,
        claim: ClaimHandle,
        invocation_id: int,
        error_code: str,
        error_message: str,
        now: datetime,
    ) -> bool:
        """Close a started call whose outcome is genuinely unknown.

        It is never closed as `failed`, because `failed` would claim the call concluded without
        effect, and never as `cancelled`, because cancellation is not an outcome a tool had.
        """

        def operation(connection: Connection) -> bool:
            if not self._conclude(connection, claim=claim, now=now):
                return False
            updated = connection.execute(
                update(ToolInvocationRecord)
                .where(
                    ToolInvocationRecord.id == invocation_id,
                    ToolInvocationRecord.attempt_id == claim.attempt_id,
                    ToolInvocationRecord.status == InvocationStatus.STARTED.value,
                )
                .values(
                    status=InvocationStatus.AMBIGUOUS.value,
                    finished_at=now,
                    error_code=error_code,
                    error_message=error_message,
                )
            )
            if _rowcount(updated) != 1:
                raise _Fenced
            self._append_tool_event(
                connection,
                claim=claim,
                event_type=RunEventType.TOOL_AMBIGUOUS,
                tool_invocation_id=invocation_id,
                code=error_code,
                message=error_message,
                now=now,
            )
            return True

        return self._fenced(operation)

    def record_pre_dispatch_refusal(self, *, claim: ClaimHandle, now: datetime) -> RefusalOutcome:
        """Record a refused call that provably never became a durable invocation.

        Three refusals happen before the loop may insert anything: the model named a tool that is
        not in the frozen catalog, the arguments were not a JSON object, and the arguments did not
        satisfy the tool's canonical input schema. Each is a real audit fact about a call the model
        made, but none can truthfully carry a `tool_invocations` row -- that table requires a real
        `tool_definition_id`, and inventing a sentinel definition, a fake id, or another Run's
        definition id would make the durable record assert something untrue.

        So the fact is recorded where it is honestly representable: one `tool.denied` event with a
        NULL `tool_invocation_id`, meaning "this Run saw a refused call that never existed as a
        row". The write is fenced on exactly the authority every other invocation write uses, so a
        Worker that lost its claim, whose lease lapsed, or whose Run was cancelled cannot add audit
        facts to a timeline it no longer owns.

        The typed outcome is what the loop needs to keep the two refusals apart: a **cancelled** Run
        means the ordinary cancellation path, while a **fenced** write means this Worker's authority
        is gone and nothing more may be dispatched or observed.
        """

        def operation(connection: Connection) -> RefusalOutcome:
            authority = self._authority(connection, claim=claim, now=now)
            if not authority.live:
                return RefusalOutcome(RefusalOutcomeKind.FENCED)
            if authority.cancelled:
                return RefusalOutcome(RefusalOutcomeKind.CANCELLED)
            self._append_tool_event(
                connection,
                claim=claim,
                event_type=RunEventType.TOOL_DENIED,
                tool_invocation_id=None,
                code=TOOL_DENIED,
                message=safe_error_message(TOOL_DENIED),
                now=now,
            )
            return RefusalOutcome(RefusalOutcomeKind.RECORDED)

        return self._runner.run(operation)

    def _append_tool_event(
        self,
        connection: Connection,
        *,
        claim: ClaimHandle,
        event_type: RunEventType,
        now: datetime,
        tool_invocation_id: int | None = None,
        code: str | None = None,
        message: str | None = None,
    ) -> None:
        """Append one tool Run Event into the caller's transition transaction.

        One event per transition, so the sequence is the Run's high-water mark plus one, read
        inside the same transaction that performed the transition. The event therefore commits
        with its cause or not at all, and a repeated transition -- which its compare-and-set
        already refuses -- can never append a second event.
        """
        append_event_on_connection(
            connection,
            run_id=claim.run_id,
            job_id=claim.job_id,
            attempt_id=claim.attempt_id,
            tool_invocation_id=tool_invocation_id,
            sequence=sequence_base(connection, claim.run_id) + 1,
            event_type=event_type,
            code=code,
            message=message,
            attempt_number=claim.attempt_number,
            created_at=now,
        )

    @staticmethod
    def _close_non_dispatched(
        connection: Connection,
        invocation_id: int,
        *,
        attempt_id: int,
        status: InvocationStatus,
        decision: PermissionDecision | None,
        now: datetime,
    ) -> Any:
        """Close a call that never crossed the start boundary.

        The row is bound to the claim's own Attempt as well as to its id, so the statement itself --
        not merely the id's uniqueness -- is what proves this Worker is closing its own call.
        """
        values: dict[str, Any] = {"status": status.value, "finished_at": now}
        if decision is not None:
            # The durable decision is restated from the live check that actually refused the call,
            # so an earlier `allowed` cannot survive beside a `denied` status.
            values["permission_decision"] = permission_decision_value(decision)
        return connection.execute(
            update(ToolInvocationRecord)
            .where(
                ToolInvocationRecord.id == invocation_id,
                ToolInvocationRecord.attempt_id == attempt_id,
                ToolInvocationRecord.status == InvocationStatus.REQUESTED.value,
            )
            .values(**values)
        )

    def _conclude(self, connection: Connection, *, claim: ClaimHandle, now: datetime) -> bool:
        """Shared fence for every transition out of `started`.

        A cancellation request revokes permission to keep going, so it also blocks a terminal write:
        a started call in a cancelled Run is left exactly as it is, and the reconciliation that
        decides what such a row means belongs to a later milestone rather than to this Worker.
        """
        authority = self._authority(connection, claim=claim, now=now)
        return authority.live and not authority.cancelled

    @staticmethod
    def _authority(connection: Connection, *, claim: ClaimHandle, now: datetime) -> _Authority:
        """Read this claim's current authority over this Attempt in one query.

        The two questions are deliberately kept apart. A claim that is no longer live means this
        Worker has no business writing at all; a live claim on a cancelled Job means the Worker may
        still record *why* a call did not happen, but may not claim it was dispatched.

        Every condition is one the durable engine already treats as authority: the right Job and
        Run, a live lease, a running Attempt that has crossed the start boundary, and both copies of
        the claim token -- the Attempt's recorded one and the Job's current one. A reclaim replaces
        the latter and appends a new Attempt, so checking only the Attempt would let a Worker that
        lost its Job keep writing to it.
        """
        row = (
            connection.execute(
                select(
                    JobAttemptRecord.status,
                    JobAttemptRecord.execution_started_at,
                    JobAttemptRecord.lease_expires_at,
                    JobRecord.cancel_requested_at.is_not(None).label("cancelled"),
                )
                .join(JobRecord, JobRecord.id == JobAttemptRecord.job_id)
                .where(
                    JobAttemptRecord.id == claim.attempt_id,
                    JobAttemptRecord.job_id == claim.job_id,
                    JobAttemptRecord.worker_id == claim.worker_id,
                    JobAttemptRecord.claim_token == claim.claim_token,
                    JobRecord.run_id == claim.run_id,
                    JobRecord.status == JobStatus.RUNNING.value,
                    JobRecord.claimed_by == claim.worker_id,
                    JobRecord.claim_token == claim.claim_token,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return _Authority(live=False, cancelled=False)
        live = (
            row["status"] == AttemptStatus.RUNNING.value
            and row["execution_started_at"] is not None
            and bool(row["lease_expires_at"] > now)
        )
        return _Authority(live=live, cancelled=bool(row["cancelled"]))

    def _authorized(self, connection: Connection, *, claim: ClaimHandle, now: datetime) -> bool:
        """Return whether this claim may write at all, which cancellation also forbids."""
        authority = self._authority(connection, claim=claim, now=now)
        return authority.live and not authority.cancelled

    def _fenced(self, operation: Callable[[Connection], bool]) -> bool:
        """Run one operation, mapping a lost fence to a plain refusal rather than an error."""
        try:
            return self._runner.run(operation)
        except (_Fenced, EventOwnershipViolation):
            # `EventOwnershipViolation` means a tool event named an invocation that is not the one
            # being audited. It cannot arise from a race -- the caller states every identifier --
            # so it is a call-site defect, and the whole transaction, transition included, rolled
            # back. Refusing is the safe reading: nothing was written.
            return False


def _rowcount(result: Any) -> int:
    count = getattr(result, "rowcount", None)
    return count if isinstance(count, int) else 0
