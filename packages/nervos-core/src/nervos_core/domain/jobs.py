"""C1 durable execution domain values and lifecycle validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class InvalidJob(ValueError):
    """A durable execution value violates the frozen C1 contract."""


class JobStatus(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptStatus(StrEnum):
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class RetryDisposition(StrEnum):
    SAFE_TO_RETRY = "SAFE_TO_RETRY"
    DO_NOT_RETRY = "DO_NOT_RETRY"
    AMBIGUOUS = "AMBIGUOUS"


class RunEventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_QUEUED = "run.queued"
    ATTEMPT_CLAIMED = "attempt.claimed"
    ATTEMPT_STARTED = "attempt.started"
    ATTEMPT_FAILED = "attempt.failed"
    ATTEMPT_EXPIRED = "attempt.expired"
    RETRY_SCHEDULED = "retry.scheduled"
    CANCELLATION_REQUESTED = "cancellation.requested"
    RUN_CANCELLED = "run.cancelled"
    RUN_SUCCEEDED = "run.succeeded"
    RUN_FAILED = "run.failed"
    RECOVERY_PRE_START = "recovery.pre_start"
    RECOVERY_AMBIGUOUS = "recovery.ambiguous"
    # The six Stage D tool types. `0007` has accepted these strings since D1 and the `run_events`
    # CHECK has always allowed them; they are listed here so the domain can represent what the
    # schema already stores, which every reader of a durable event requires.
    #
    # `tool.cancelled` is deliberately absent, exactly as ADR 0017 states: cancellation is a Run
    # lifecycle the user requested, `cancellation.requested` and `run.cancelled` carry it, and a
    # tool that was stopped before dispatch says so on its own durable row. Inventing a per-tool
    # cancellation event would create a second, weaker source of the same truth.
    TOOL_REQUESTED = "tool.requested"
    TOOL_STARTED = "tool.started"
    TOOL_SUCCEEDED = "tool.succeeded"
    TOOL_FAILED = "tool.failed"
    TOOL_DENIED = "tool.denied"
    TOOL_AMBIGUOUS = "tool.ambiguous"


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    run_id: int
    agent_instance_id: int
    model_provider: str
    status: JobStatus
    available_at: datetime
    attempt_count: int
    max_attempts: int
    cancel_requested_at: datetime | None
    claimed_by: str | None
    claim_token: bytes | None
    lease_expires_at: datetime | None
    last_heartbeat_at: datetime | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None

    def __post_init__(self) -> None:
        if min(self.id, self.run_id, self.agent_instance_id) <= 0:
            raise InvalidJob("identifiers must be positive")
        if not self.model_provider or len(self.model_provider) > 64:
            raise InvalidJob("invalid model provider")
        if not 1 <= self.max_attempts <= 10 or not 0 <= self.attempt_count <= self.max_attempts:
            raise InvalidJob("invalid attempt budget")
        for name in (
            "available_at",
            "created_at",
            "updated_at",
            "cancel_requested_at",
            "lease_expires_at",
            "last_heartbeat_at",
            "finished_at",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value))
        lease = (
            self.claimed_by,
            self.claim_token,
            self.lease_expires_at,
            self.last_heartbeat_at,
        )
        if any(value is None for value in lease) != all(value is None for value in lease):
            raise InvalidJob("claim fields must be all present or all absent")
        if self.claim_token is not None and len(self.claim_token) != 32:
            raise InvalidJob("claim token must contain 32 bytes")
        if (self.error_code is None) != (self.error_message is None):
            raise InvalidJob("error fields must be paired")
        terminal = self.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
        if terminal != (self.finished_at is not None):
            raise InvalidJob("terminal timestamp does not match status")
        if terminal and any(value is not None for value in lease):
            raise InvalidJob("terminal jobs cannot retain a claim")
        if self.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
            _validate_error(self.error_code, self.error_message)
        elif self.error_code is not None:
            raise InvalidJob("only failed or cancelled jobs carry errors")
        if self.status is JobStatus.CANCELLED and self.cancel_requested_at is None:
            raise InvalidJob("cancelled job requires cancellation request")


@dataclass(frozen=True, slots=True)
class JobAttempt:
    id: int
    job_id: int
    attempt_number: int
    status: AttemptStatus
    worker_id: str
    claim_token: bytes
    claimed_at: datetime
    execution_started_at: datetime | None
    lease_expires_at: datetime
    last_heartbeat_at: datetime
    finished_at: datetime | None
    retry_disposition: RetryDisposition | None
    error_code: str | None
    error_message: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        if min(self.id, self.job_id, self.attempt_number) <= 0:
            raise InvalidJob("identifiers must be positive")
        if not self.worker_id or len(self.worker_id) > 128 or len(self.claim_token) != 32:
            raise InvalidJob("invalid attempt owner")
        for name in (
            "claimed_at",
            "execution_started_at",
            "lease_expires_at",
            "last_heartbeat_at",
            "finished_at",
            "created_at",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value))
        if self.lease_expires_at <= self.claimed_at or self.last_heartbeat_at < self.claimed_at:
            raise InvalidJob("invalid lease order")
        if self.execution_started_at and self.execution_started_at < self.claimed_at:
            raise InvalidJob("invalid execution start")
        if self.finished_at and self.finished_at < (self.execution_started_at or self.claimed_at):
            raise InvalidJob("invalid finish order")
        terminal = self.status in {
            AttemptStatus.SUCCEEDED,
            AttemptStatus.FAILED,
            AttemptStatus.CANCELLED,
            AttemptStatus.EXPIRED,
        }
        if terminal != (self.finished_at is not None):
            raise InvalidJob("terminal timestamp does not match status")
        if not terminal and (self.retry_disposition is not None or self.error_code is not None):
            raise InvalidJob("active attempts cannot have outcomes")
        if self.status is AttemptStatus.RUNNING and self.execution_started_at is None:
            raise InvalidJob("running attempt requires execution start")
        if self.status is AttemptStatus.CLAIMED and self.execution_started_at is not None:
            raise InvalidJob("claimed attempt has not started")
        if (
            self.status in {AttemptStatus.FAILED, AttemptStatus.EXPIRED}
            and self.retry_disposition is None
        ):
            raise InvalidJob("failure requires retry disposition")
        if self.status is AttemptStatus.SUCCEEDED and (
            self.retry_disposition is not None or self.error_code is not None
        ):
            raise InvalidJob("success cannot carry an error outcome")
        if (self.error_code is None) != (self.error_message is None):
            raise InvalidJob("error fields must be paired")
        if self.error_code is not None:
            _validate_error(self.error_code, self.error_message)


@dataclass(frozen=True, slots=True)
class RunEvent:
    id: int
    run_id: int
    job_id: int
    attempt_id: int | None
    sequence: int
    event_type: RunEventType
    code: str | None
    message: str | None
    attempt_number: int | None
    available_at: datetime | None
    created_at: datetime
    # The durable ToolInvocation a tool event is about, or ``None``. It is nullable because the
    # pre-dispatch refusals -- an unknown tool, malformed arguments, arguments a canonical schema
    # rejected -- are real audit facts about a call that provably never existed as a row, and
    # inventing a sentinel invocation or a fake definition id for them would make a durable lie
    # out of a truthful gap.
    tool_invocation_id: int | None = None

    def __post_init__(self) -> None:
        if min(self.id, self.run_id, self.job_id, self.sequence) <= 0:
            raise InvalidJob("event identifiers and sequence must be positive")
        if self.attempt_id is not None and self.attempt_id <= 0:
            raise InvalidJob("invalid attempt identifier")
        if self.attempt_number is not None and self.attempt_number <= 0:
            raise InvalidJob("invalid attempt number")
        if self.tool_invocation_id is not None and self.tool_invocation_id <= 0:
            raise InvalidJob("invalid tool invocation identifier")
        if (self.code is None) != (self.message is None):
            raise InvalidJob("event code and message must be paired")
        if self.code is not None:
            _validate_error(self.code, self.message)
        object.__setattr__(self, "created_at", _utc(self.created_at))
        if self.available_at is not None:
            object.__setattr__(self, "available_at", _utc(self.available_at))


def _validate_error(code: str | None, message: str | None) -> None:
    if code is None or message is None or _SAFE_CODE.fullmatch(code) is None:
        raise InvalidJob("invalid safe error")
    if not message.strip() or len(message) > 512 or "\x00" in message:
        raise InvalidJob("invalid safe error")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidJob("timestamp must be timezone-aware")
    return value.astimezone(UTC)
