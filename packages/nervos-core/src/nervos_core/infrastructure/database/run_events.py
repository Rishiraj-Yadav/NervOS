"""The one primitive that appends a durable Run Event onto a caller's transaction.

Every Run Event in NervOS is written through this module. It exists because two different
persistence seams now need the same three things and neither may own them privately:

* **The per-Run sequence high-water read.** `run_events` carries `UNIQUE(run_id, sequence)`, so a
  transaction that appends several events must read the mark *once* and hand out a contiguous
  batch. Two independent `MAX(sequence) + 1` reads inside one transaction would allocate the same
  value and fail the constraint, so the read is a named, shared step rather than something each
  caller re-derives.

* **The insert itself, with no transaction management.** This module never opens, commits, or
  rolls back anything. The caller's transaction is authoritative: an event is a fact about a
  transition that committed, so it must commit or roll back as part of that same transition and
  never as a separate write that could survive its cause or vanish while its cause stands.

* **The ownership proof for a tool event.** `run_events.tool_invocation_id` is a foreign key, and
  a foreign key proves only that *an* invocation with that id exists -- not that it is this Run's,
  this Job's, this Attempt's. A foreign key is therefore necessary but not sufficient, and the
  check is performed here so no caller can forget it.

Nothing in this module decides whether an event *should* exist. It records what a decision that
already committed produced, and it is never read back to make one: Run Events are an append-only
safe chronology, not an authorization ledger.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, insert, select
from sqlalchemy.engine import Connection

from nervos_core.domain.jobs import RunEventType
from nervos_core.infrastructure.database.models import RunEventRecord, ToolInvocationRecord


class EventOwnershipViolation(Exception):
    """A tool event named an invocation that is not the audited Run/Job/Attempt's own.

    It is an error rather than a refusal because it cannot arise from a race: the caller states
    the Run, Job, Attempt and invocation together, so a mismatch is a defect in the call site, and
    the enclosing transaction must roll back rather than persist an event linked to another Run.
    """


def sequence_base(connection: Connection, run_id: int) -> int:
    """Read the per-Run event high-water mark exactly once.

    Called once per transaction by a caller that intends to append one or more events; the batch
    it hands out is `base + 1`, `base + 2`, and so on.
    """
    return int(
        connection.scalar(
            select(func.coalesce(func.max(RunEventRecord.sequence), 0)).where(
                RunEventRecord.run_id == run_id
            )
        )
        or 0
    )


def _verify_invocation_ownership(
    connection: Connection,
    *,
    tool_invocation_id: int,
    run_id: int,
    job_id: int,
    attempt_id: int | None,
) -> None:
    """Prove the named invocation belongs to exactly the Run, Job and Attempt being audited.

    A valid invocation id from a different Run satisfies the foreign key and would still be a
    false audit fact -- it would link one Run's timeline to another Run's call -- so the triple is
    compared rather than the id's mere existence. An event about a tool call always has an Attempt,
    so a caller that supplies an invocation id without one is refused rather than accommodated.
    """
    if attempt_id is None:
        raise EventOwnershipViolation("a tool event must name the Attempt that owns the call")
    row = connection.execute(
        select(
            ToolInvocationRecord.run_id,
            ToolInvocationRecord.job_id,
            ToolInvocationRecord.attempt_id,
        ).where(ToolInvocationRecord.id == tool_invocation_id)
    ).one_or_none()
    if row is None:
        raise EventOwnershipViolation("a tool event named an invocation that does not exist")
    if int(row[0]) != run_id or int(row[1]) != job_id or int(row[2]) != attempt_id:
        raise EventOwnershipViolation(
            "a tool event named an invocation owned by a different run, job or attempt"
        )


def append_event_on_connection(
    connection: Connection,
    *,
    run_id: int,
    job_id: int,
    sequence: int,
    event_type: RunEventType,
    created_at: datetime,
    attempt_id: int | None = None,
    tool_invocation_id: int | None = None,
    code: str | None = None,
    message: str | None = None,
    attempt_number: int | None = None,
    available_at: datetime | None = None,
) -> None:
    """Insert one Run Event on the caller's connection without opening or committing a transaction.

    `code` and `message` are accepted because the Stage C events already carry them and their
    vocabulary is the same single allowlist the rest of the engine uses; the guarantee this module
    provides is that the *shape* is safe (a bounded code and a bounded static message), not that
    every caller's string is inherently static. The narrower rule -- that no tool event call site
    ever derives a code or message from an exception, a server response, a tool result, a
    credential, or user data -- is a property of those call sites and is guarded there.
    """
    if tool_invocation_id is not None:
        _verify_invocation_ownership(
            connection,
            tool_invocation_id=tool_invocation_id,
            run_id=run_id,
            job_id=job_id,
            attempt_id=attempt_id,
        )
    connection.execute(
        insert(RunEventRecord).values(
            run_id=run_id,
            job_id=job_id,
            attempt_id=attempt_id,
            tool_invocation_id=tool_invocation_id,
            sequence=sequence,
            event_type=event_type.value,
            code=code,
            message=message,
            attempt_number=attempt_number,
            available_at=available_at,
            created_at=created_at,
        )
    )


__all__ = [
    "EventOwnershipViolation",
    "append_event_on_connection",
    "sequence_base",
]
