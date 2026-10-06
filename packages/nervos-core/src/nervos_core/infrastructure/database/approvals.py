"""SQLAlchemy persistence for Stage H3 action approvals (ADR 0034)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy import Connection, Engine, insert, select, update
from sqlalchemy.engine import RowMapping

from nervos_core.application.approvals import (
    CONSUMED,
    CONSUMPTION_UNAVAILABLE,
    ActionApproval,
    ApprovalAdmission,
    ApprovalConflict,
    ApprovalNotFound,
    ApprovalRequest,
    ConsumeOutcome,
    action_fingerprint,
    input_digest,
    safe_preview,
)
from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.application.tool_invocations import ClaimHandle, StartOutcome
from nervos_core.domain.jobs import RunEventType
from nervos_core.domain.tools import JsonValue
from nervos_core.infrastructure.database.models import (
    ActionApprovalRecord,
    AgentInstanceRecord,
    JobAttemptRecord,
    JobRecord,
    RunRecord,
    ToolInvocationRecord,
)
from nervos_core.infrastructure.database.run_events import append_event_on_connection, sequence_base
from nervos_core.infrastructure.database.transaction import TransactionRunner


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _decode_preview(preview_json: str) -> dict[str, JsonValue]:
    """Decode the stored preview. A malformed blob renders as an empty preview rather
    than failing the owner's view — the row's state is authoritative, not its rendering."""
    try:
        decoded: object = json.loads(preview_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    preview: dict[str, JsonValue] = {}
    mapping = cast("dict[object, object]", decoded)
    for raw_key, raw_value in mapping.items():
        if isinstance(raw_key, str):
            preview[raw_key] = cast("JsonValue", raw_value)
    return preview


def _by_id(conn: Connection, approval_id: int) -> RowMapping:
    row = (
        conn.execute(select(ActionApprovalRecord).where(ActionApprovalRecord.id == approval_id))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ApprovalNotFound
    return row


def _resolved_owner(conn: Connection, *, agent_instance_id: int, run_id: int, job_id: int) -> int:
    """Resolve the owner from durable rows, or refuse.

    Three joins, one answer: the Agent Instance owns the Run, the Run owns the Job. A caller
    cannot name an owner, so an approval can never be filed against somebody else's instance.
    """
    instance = (
        conn.execute(
            select(AgentInstanceRecord.owner_user_id).where(
                AgentInstanceRecord.id == agent_instance_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if instance is None:
        raise ApprovalNotFound
    run = (
        conn.execute(
            select(RunRecord.id).where(
                RunRecord.id == run_id, RunRecord.agent_instance_id == agent_instance_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if run is None:
        raise ApprovalNotFound
    job = (
        conn.execute(select(JobRecord.id).where(JobRecord.id == job_id, JobRecord.run_id == run_id))
        .mappings()
        .one_or_none()
    )
    if job is None:
        raise ApprovalNotFound
    return int(instance["owner_user_id"])


def _insert_pending(
    conn: Connection, command: ApprovalRequest, now: datetime, *, owner_user_id: int
) -> int:
    """Insert one `pending` row inside the caller's open transaction.

    Shared by the persistence port and the gate so the gate never opens a second transaction
    while it holds one -- a nested `BEGIN IMMEDIATE` on SQLite is a deadlock, not a detail.
    """
    cursor = conn.execute(
        insert(ActionApprovalRecord).values(
            owner_user_id=owner_user_id,
            agent_instance_id=command.agent_instance_id,
            run_id=command.run_id,
            job_id=command.job_id,
            attempt_id=command.attempt_id,
            tool_sequence=command.tool_sequence,
            tool_definition_id=command.tool_definition_id,
            upstream_name=command.upstream_name,
            fingerprint=action_fingerprint(
                tool_definition_id=command.tool_definition_id,
                upstream_name=command.upstream_name,
                fingerprint=command.fingerprint,
            ),
            input_digest=input_digest(command.arguments),
            preview_json=json.dumps(safe_preview(command.arguments), separators=(",", ":")),
            state="pending",
            requested_at=now,
            expires_at=now + timedelta(seconds=command.expires_in_seconds),
        )
    )
    inserted = cursor.inserted_primary_key
    if inserted is None or inserted[0] is None:  # pragma: no cover - always reported
        raise PersistenceUnavailable
    approval_id = int(inserted[0])
    _approval_event(conn, _by_id(conn, approval_id), RunEventType.TOOL_APPROVAL_REQUESTED, now)
    return approval_id


def _approval_event(conn: Connection, row: RowMapping, event: RunEventType, now: datetime) -> None:
    invocation_id = conn.scalar(
        select(ToolInvocationRecord.id).where(
            ToolInvocationRecord.run_id == row["run_id"],
            ToolInvocationRecord.attempt_id == row["attempt_id"],
            ToolInvocationRecord.tool_sequence == row["tool_sequence"],
        )
    )
    append_event_on_connection(
        conn,
        run_id=int(row["run_id"]),
        job_id=int(row["job_id"]),
        attempt_id=int(row["attempt_id"]),
        tool_invocation_id=int(invocation_id) if invocation_id is not None else None,
        sequence=sequence_base(conn, int(row["run_id"])) + 1,
        event_type=event,
        created_at=now,
    )


def _view(row: ActionApprovalRecord | RowMapping) -> ActionApproval:
    preview_json = str(row["preview_json"] if isinstance(row, RowMapping) else row.preview_json)
    return ActionApproval(
        id=int(row["id"] if isinstance(row, RowMapping) else row.id),
        owner_user_id=int(
            row["owner_user_id"] if isinstance(row, RowMapping) else row.owner_user_id
        ),
        agent_instance_id=int(
            row["agent_instance_id"] if isinstance(row, RowMapping) else row.agent_instance_id
        ),
        run_id=int(row["run_id"] if isinstance(row, RowMapping) else row.run_id),
        job_id=int(row["job_id"] if isinstance(row, RowMapping) else row.job_id),
        attempt_id=int(row["attempt_id"] if isinstance(row, RowMapping) else row.attempt_id),
        tool_sequence=int(
            row["tool_sequence"] if isinstance(row, RowMapping) else row.tool_sequence
        ),
        tool_definition_id=int(
            row["tool_definition_id"] if isinstance(row, RowMapping) else row.tool_definition_id
        ),
        upstream_name=str(
            row["upstream_name"] if isinstance(row, RowMapping) else row.upstream_name
        ),
        fingerprint=str(row["fingerprint"] if isinstance(row, RowMapping) else row.fingerprint),
        input_digest=str(row["input_digest"] if isinstance(row, RowMapping) else row.input_digest),
        preview=_decode_preview(preview_json),
        state=str(row["state"] if isinstance(row, RowMapping) else row.state),
        requested_at=(row["requested_at"] if isinstance(row, RowMapping) else row.requested_at),
        expires_at=row["expires_at"] if isinstance(row, RowMapping) else row.expires_at,
        decided_at=row["decided_at"] if isinstance(row, RowMapping) else row.decided_at,
        consumed_at=(row["consumed_at"] if isinstance(row, RowMapping) else row.consumed_at),
    )


def reconcile_pending(conn: Connection, owner: int, now: datetime) -> None:
    """Expire or cancel stale owner approvals; each transition has a safe audit event."""
    rows = (
        conn.execute(
            select(ActionApprovalRecord).where(
                ActionApprovalRecord.owner_user_id == owner,
                ActionApprovalRecord.state.in_(("pending", "approved")),
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        state = "expired" if row["expires_at"] <= now else None
        if state is None:
            live = conn.scalar(
                select(JobAttemptRecord.id)
                .join(
                    JobRecord,
                    JobRecord.id == JobAttemptRecord.job_id,
                )
                .where(
                    JobAttemptRecord.id == row["attempt_id"],
                    JobAttemptRecord.job_id == row["job_id"],
                    JobAttemptRecord.status == "running",
                    JobAttemptRecord.execution_started_at.is_not(None),
                    JobAttemptRecord.lease_expires_at > now,
                    JobRecord.run_id == row["run_id"],
                    JobRecord.status == "running",
                    JobRecord.cancel_requested_at.is_(None),
                    JobRecord.claim_token == JobAttemptRecord.claim_token,
                )
            )
            if live is None:
                state = "cancelled"
        if state is not None:
            SqlAlchemyApprovalPersistence.set_state_on_connection(
                conn, int(row["id"]), state, now, decided=True
            )


class SqlAlchemyApprovalPersistence:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._transactions = TransactionRunner(engine, _sleep)

    def request(self, command: ApprovalRequest, now: datetime) -> ActionApproval:
        def write(conn: Connection) -> ActionApproval:
            owner_user_id = _resolved_owner(
                conn,
                agent_instance_id=command.agent_instance_id,
                run_id=command.run_id,
                job_id=command.job_id,
            )
            approval_id = _insert_pending(conn, command, now, owner_user_id=owner_user_id)
            return _view(_by_id(conn, approval_id))

        return self._transactions.run(write)

    def approve(self, *, owner_user_id: int, approval_id: int, now: datetime) -> ActionApproval:
        def write(conn: Connection) -> ActionApproval:
            row = self._owned(conn, owner_user_id, approval_id)
            if row["state"] in ("pending", "approved"):
                reconcile_pending(conn, owner_user_id, now)
                row = self._owned(conn, owner_user_id, approval_id)
            if row["state"] in ("expired", "cancelled"):
                return _view(row)
            if row["state"] == "approved":
                return _view(row)  # idempotent re-approval
            if row["state"] != "pending":
                raise ApprovalConflict
            expires_at = row["expires_at"]
            if expires_at is not None and expires_at <= now:
                self.set_state_on_connection(conn, approval_id, "expired", now, decided=True)
                return _view(self._owned(conn, owner_user_id, approval_id))
            self.set_state_on_connection(conn, approval_id, "approved", now, decided=True)
            return _view(self._owned(conn, owner_user_id, approval_id))

        result = self._transactions.run(write)
        if result.state in ("expired", "cancelled"):
            raise ApprovalConflict
        return result

    def deny(self, *, owner_user_id: int, approval_id: int, now: datetime) -> ActionApproval:
        def write(conn: Connection) -> ActionApproval:
            row = self._owned(conn, owner_user_id, approval_id)
            if row["state"] == "denied":
                return _view(row)
            if row["state"] != "pending":
                raise ApprovalConflict
            self.set_state_on_connection(conn, approval_id, "denied", now, decided=True)
            return _view(self._owned(conn, owner_user_id, approval_id))

        return self._transactions.run(write)

    def cancel(self, *, owner_user_id: int, approval_id: int, now: datetime) -> ActionApproval:
        def write(conn: Connection) -> ActionApproval:
            row = self._owned(conn, owner_user_id, approval_id)
            if row["state"] == "cancelled":
                return _view(row)
            if row["state"] != "pending":
                raise ApprovalConflict
            self.set_state_on_connection(conn, approval_id, "cancelled", now, decided=True)
            return _view(self._owned(conn, owner_user_id, approval_id))

        return self._transactions.run(write)

    def get(self, *, owner_user_id: int, approval_id: int) -> ActionApproval:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    select(ActionApprovalRecord).where(
                        ActionApprovalRecord.id == approval_id,
                        ActionApprovalRecord.owner_user_id == owner_user_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ApprovalNotFound
        return _view(row)

    def list_pending(
        self, *, owner_user_id: int, now: datetime | None = None
    ) -> tuple[ActionApproval, ...]:
        if now is not None:
            self._transactions.run(lambda conn: reconcile_pending(conn, owner_user_id, now))
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    select(ActionApprovalRecord)
                    .where(
                        ActionApprovalRecord.owner_user_id == owner_user_id,
                        ActionApprovalRecord.state == "pending",
                    )
                    .order_by(ActionApprovalRecord.id)
                )
                .mappings()
                .all()
            )
        return tuple(_view(row) for row in rows)

    # -- internal helpers ------------------------------------------------------------------------

    @staticmethod
    def _owned(conn: Connection, owner_user_id: int, approval_id: int) -> RowMapping:
        row = _by_id(conn, approval_id)
        if int(row["owner_user_id"]) != owner_user_id:
            raise ApprovalNotFound
        return row

    @staticmethod
    def set_state_on_connection(
        conn: Connection,
        approval_id: int,
        state: str,
        decided_at: datetime,
        *,
        decided: bool,
    ) -> None:
        old = _by_id(conn, approval_id)
        conn.execute(
            update(ActionApprovalRecord)
            .where(ActionApprovalRecord.id == approval_id)
            .values(state=state, decided_at=decided_at if decided else None)
        )
        if old["state"] != state:
            _approval_event(conn, old, RunEventType.TOOL_APPROVAL_DECIDED, decided_at)


class SqlAlchemyApprovalGate:
    """The mediator's approval port (ADR 0034).

    ``admit`` is the pre-dispatch check and ``consume`` is the dispatch-time compare-and-set.
    Both run inside one bounded transaction each and neither holds a connection across an await,
    so neither can widen the Stage-D rule that no transaction is ever open across a tool call.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._transactions = TransactionRunner(engine, _sleep)

    def start(
        self, request: ApprovalRequest, *, claim: ClaimHandle, invocation_id: int, now: datetime
    ) -> StartOutcome:
        from nervos_core.infrastructure.database.tool_invocations import (
            SqlAlchemyToolInvocationPersistence,
        )

        return SqlAlchemyToolInvocationPersistence(self._engine).mark_started(
            claim=claim, invocation_id=invocation_id, now=now, approval=request
        )

    def abandon(self, request: ApprovalRequest, *, now: datetime) -> None:
        def write(conn: Connection) -> None:
            rows = (
                conn.execute(
                    select(ActionApprovalRecord).where(
                        ActionApprovalRecord.run_id == request.run_id,
                        ActionApprovalRecord.attempt_id == request.attempt_id,
                        ActionApprovalRecord.tool_sequence == request.tool_sequence,
                        ActionApprovalRecord.state.in_(("pending", "approved")),
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                SqlAlchemyApprovalPersistence.set_state_on_connection(
                    conn,
                    int(row["id"]),
                    "cancelled",
                    now,
                    decided=True,
                )

        self._transactions.run(write)

    def admit(self, request: ApprovalRequest, *, now: datetime) -> ApprovalAdmission:
        digest = input_digest(request.arguments)
        fingerprint = action_fingerprint(
            tool_definition_id=request.tool_definition_id,
            upstream_name=request.upstream_name,
            fingerprint=request.fingerprint,
        )

        def read(conn: Connection) -> ApprovalAdmission:
            try:
                owner_user_id = _resolved_owner(
                    conn,
                    agent_instance_id=request.agent_instance_id,
                    run_id=request.run_id,
                    job_id=request.job_id,
                )
            except ApprovalNotFound:
                # The Run, Job, or Agent Instance this call claims to belong to is not durable.
                # Authority cannot be confirmed, so nothing is requested and nothing is allowed.
                return ApprovalAdmission.UNAVAILABLE
            reconcile_pending(conn, owner_user_id, now)
            if not _live_request(conn, request, now):
                return ApprovalAdmission.REFUSED
            row = _matching(
                conn,
                owner_user_id=owner_user_id,
                run_id=request.run_id,
                job_id=request.job_id,
                attempt_id=request.attempt_id,
                tool_sequence=request.tool_sequence,
                tool_definition_id=request.tool_definition_id,
                upstream_name=request.upstream_name,
                fingerprint=fingerprint,
                input_digest=digest,
            )
            if row is None:
                try:
                    _insert_pending(conn, request, now, owner_user_id=owner_user_id)
                except PersistenceUnavailable:
                    return ApprovalAdmission.UNAVAILABLE
                return ApprovalAdmission.REQUESTED
            state = str(row["state"])
            if state == "approved":
                expires_at = row["expires_at"]
                if expires_at is None or expires_at > now:
                    return ApprovalAdmission.APPROVED
                # An approval nobody answered in time is terminal, not "still approved".
                self._expire(conn, int(row["id"]), now)
                return ApprovalAdmission.REFUSED
            if state == "pending":
                if row["expires_at"] <= now:
                    self._expire(conn, int(row["id"]), now)
                    return ApprovalAdmission.REFUSED
                return ApprovalAdmission.REQUESTED
            # denied | expired | cancelled | revoked | consumed: terminal, zero dispatches, and
            # no second request for the same exact action in the same Run.
            return ApprovalAdmission.REFUSED

        return self._transactions.run(read)

    def consume(
        self, request: ApprovalRequest, *, consuming_attempt_id: int, now: datetime
    ) -> ConsumeOutcome:
        if consuming_attempt_id != request.attempt_id:
            return ConsumeOutcome(CONSUMPTION_UNAVAILABLE)
        digest = input_digest(request.arguments)
        fingerprint = action_fingerprint(
            tool_definition_id=request.tool_definition_id,
            upstream_name=request.upstream_name,
            fingerprint=request.fingerprint,
        )

        def write(conn: Connection) -> ConsumeOutcome:
            if not _live_request(conn, request, now):
                return ConsumeOutcome(CONSUMPTION_UNAVAILABLE)
            try:
                owner_user_id = _resolved_owner(
                    conn,
                    agent_instance_id=request.agent_instance_id,
                    run_id=request.run_id,
                    job_id=request.job_id,
                )
            except ApprovalNotFound:
                return ConsumeOutcome(CONSUMPTION_UNAVAILABLE)
            row = _matching(
                conn,
                owner_user_id=owner_user_id,
                run_id=request.run_id,
                job_id=request.job_id,
                attempt_id=request.attempt_id,
                tool_sequence=request.tool_sequence,
                tool_definition_id=request.tool_definition_id,
                upstream_name=request.upstream_name,
                fingerprint=fingerprint,
                input_digest=digest,
                state="approved",
            )
            if row is None:
                return ConsumeOutcome(CONSUMPTION_UNAVAILABLE)
            expires_at = row["expires_at"]
            if expires_at is None or expires_at <= now:
                self._expire(conn, int(row["id"]), now)
                return ConsumeOutcome(CONSUMPTION_UNAVAILABLE)
            approval_id = int(row["id"])
            # The compare-and-set: two Workers racing for one approval produce exactly one
            # winner, and the loser dispatches nothing.
            claimed = conn.execute(
                update(ActionApprovalRecord)
                .where(
                    ActionApprovalRecord.id == approval_id,
                    ActionApprovalRecord.state == "approved",
                    ActionApprovalRecord.consumed_at.is_(None),
                )
                .values(state="consumed", consumed_at=now)
            )
            if claimed.rowcount != 1:
                return ConsumeOutcome(CONSUMPTION_UNAVAILABLE)
            return ConsumeOutcome(CONSUMED)

        return self._transactions.run(write)

    @staticmethod
    def _expire(conn: Connection, approval_id: int, now: datetime) -> None:
        row = _by_id(conn, approval_id)
        if row["state"] in ("approved", "pending"):
            SqlAlchemyApprovalPersistence.set_state_on_connection(
                conn,
                approval_id,
                "expired",
                now,
                decided=True,
            )


def _matching(
    conn: Connection,
    *,
    owner_user_id: int,
    run_id: int,
    job_id: int,
    attempt_id: int,
    tool_sequence: int,
    tool_definition_id: int,
    upstream_name: str,
    fingerprint: str,
    input_digest: str,
    state: str | None = None,
) -> RowMapping | None:
    """The one row bound to exactly this action, if one exists.

    The match is the full identity ADR 0034 freezes: owner, Run, Job, sequence, tool identity,
    reviewed-definition action fingerprint, and the digest of the exact input. Anything less
    would let one approval authorize a different call.
    """
    clauses = [
        ActionApprovalRecord.owner_user_id == owner_user_id,
        ActionApprovalRecord.run_id == run_id,
        ActionApprovalRecord.job_id == job_id,
        ActionApprovalRecord.attempt_id == attempt_id,
        ActionApprovalRecord.tool_sequence == tool_sequence,
        ActionApprovalRecord.tool_definition_id == tool_definition_id,
        ActionApprovalRecord.upstream_name == upstream_name,
        ActionApprovalRecord.fingerprint == fingerprint,
        ActionApprovalRecord.input_digest == input_digest,
    ]
    if state is not None:
        clauses.append(ActionApprovalRecord.state == state)
    return (
        conn.execute(
            select(ActionApprovalRecord)
            .where(*clauses)
            .order_by(ActionApprovalRecord.id.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )


def _live_request(conn: Connection, request: ApprovalRequest, now: datetime) -> bool:
    return (
        conn.execute(
            select(JobAttemptRecord.id)
            .join(JobRecord, JobRecord.id == JobAttemptRecord.job_id)
            .where(
                JobAttemptRecord.id == request.attempt_id,
                JobAttemptRecord.job_id == request.job_id,
                JobAttemptRecord.status == "running",
                JobAttemptRecord.execution_started_at.is_not(None),
                JobAttemptRecord.lease_expires_at > now,
                JobRecord.run_id == request.run_id,
                JobRecord.status == "running",
                JobRecord.cancel_requested_at.is_(None),
                JobRecord.claim_token == JobAttemptRecord.claim_token,
            )
        ).first()
        is not None
    )


def consume_on_connection(conn: Connection, request: ApprovalRequest, now: datetime) -> bool:
    """Consume only within the live invocation-start transaction."""
    owner = _resolved_owner(
        conn,
        agent_instance_id=request.agent_instance_id,
        run_id=request.run_id,
        job_id=request.job_id,
    )
    row = _matching(
        conn,
        owner_user_id=owner,
        run_id=request.run_id,
        job_id=request.job_id,
        attempt_id=request.attempt_id,
        tool_sequence=request.tool_sequence,
        tool_definition_id=request.tool_definition_id,
        upstream_name=request.upstream_name,
        fingerprint=action_fingerprint(
            tool_definition_id=request.tool_definition_id,
            upstream_name=request.upstream_name,
            fingerprint=request.fingerprint,
        ),
        input_digest=input_digest(request.arguments),
        state="approved",
    )
    if row is None or row["expires_at"] <= now or not _live_request(conn, request, now):
        return False
    result = conn.execute(
        update(ActionApprovalRecord)
        .where(
            ActionApprovalRecord.id == row["id"],
            ActionApprovalRecord.state == "approved",
            ActionApprovalRecord.consumed_at.is_(None),
        )
        .values(state="consumed", consumed_at=now)
    )
    return result.rowcount == 1
