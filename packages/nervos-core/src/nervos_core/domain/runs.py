"""Immutable Run domain values and lifecycle validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from nervos_core.domain.jobs import JobStatus

OUTCOME_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
# The one terminal outcome code that licenses a `failed` Run with no start boundary: the Job
# exhausted its claim budget to repeated pre-start Worker losses, so execution provably never
# began. Every other failed Run must carry a real `started_at` and a real `elapsed_ms`.
WORKER_RECOVERY_EXHAUSTED = "worker_recovery_exhausted"
# Frozen to keep blank-text semantics stable across Python and SQLite.
NERVOS_BLANK_TEXT_CODE_POINTS = frozenset(
    {
        chr(0x0009),
        chr(0x000A),
        chr(0x000B),
        chr(0x000C),
        chr(0x000D),
        chr(0x0020),
        chr(0x0085),
        chr(0x00A0),
        chr(0x1680),
        chr(0x2000),
        chr(0x2001),
        chr(0x2002),
        chr(0x2003),
        chr(0x2004),
        chr(0x2005),
        chr(0x2006),
        chr(0x2007),
        chr(0x2008),
        chr(0x2009),
        chr(0x200A),
        chr(0x2028),
        chr(0x2029),
        chr(0x202F),
        chr(0x205F),
        chr(0x3000),
    }
)


def is_blank_text(value: str) -> bool:
    return not value or all(character in NERVOS_BLANK_TEXT_CODE_POINTS for character in value)


class InvalidRun(ValueError):
    """Raised when a Run violates lifecycle or snapshot invariants."""


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # A distinct terminal lifecycle, never a euphemism for `failed`: a cancelled Run carries no
    # provider error, because refusing to continue is not a provider outcome.
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RunLimits:
    input_max_bytes: int = 8000
    input_max_code_points: int = 4000
    output_max_bytes: int = 32000
    output_max_code_points: int = 16000
    provider_timeout_ms: int = 60000
    max_output_tokens: int = 1024
    max_model_calls: int = 1

    def __post_init__(self) -> None:
        if any(value <= 0 for value in self.values()):
            raise InvalidRun("execution limits must be positive")

    def values(self) -> tuple[int, ...]:
        return (
            self.input_max_bytes,
            self.input_max_code_points,
            self.output_max_bytes,
            self.output_max_code_points,
            self.provider_timeout_ms,
            self.max_output_tokens,
            self.max_model_calls,
        )


STAGE_B_LIMITS = RunLimits()


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        if any(value is not None and value < 0 for value in self.values()):
            raise InvalidRun("usage must be nonnegative")

    def values(self) -> tuple[int | None, ...]:
        return self.input_tokens, self.output_tokens, self.total_tokens


def validate_input_text(value: str, limits: RunLimits = STAGE_B_LIMITS) -> str:
    if is_blank_text(value) or "\x00" in value:
        raise InvalidRun("invalid input text")
    if (
        len(value) > limits.input_max_code_points
        or len(value.encode("utf-8")) > limits.input_max_bytes
    ):
        raise InvalidRun("input text exceeds limits")
    return value


def validate_output_text(value: str, limits: RunLimits) -> str:
    if is_blank_text(value) or "\x00" in value:
        raise InvalidRun("invalid output text")
    if (
        len(value) > limits.output_max_code_points
        or len(value.encode("utf-8")) > limits.output_max_bytes
    ):
        raise InvalidRun("output text exceeds limits")
    return value


def validate_outcome_code(value: str) -> str:
    if OUTCOME_CODE_PATTERN.fullmatch(value) is None:
        raise InvalidRun("invalid outcome code")
    return value


def validate_error_message(value: str) -> str:
    if (
        is_blank_text(value)
        or "\x00" in value
        or len(value) > 512
        or len(value.encode("utf-8")) > 2048
    ):
        raise InvalidRun("invalid safe error message")
    return value


@dataclass(frozen=True, slots=True)
class Run:
    id: int
    agent_instance_id: int
    agent_key: str
    agent_definition_version: str
    model_provider: str
    model_name: str
    input_text: str
    limits: RunLimits
    status: RunStatus
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    output_text: str | None = None
    finish_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    usage: ModelUsage = ModelUsage()
    elapsed_ms: int | None = None
    # Derived, read-only projections of the durable Job behind this Run, supplied only by the
    # owner-scoped read paths that join `jobs`. They are never persisted on the Run and carry no
    # authority: the Job lease decides what may execute, and nothing may branch on these. A
    # `running` Run waiting on a retry and a `running` Run executing now are otherwise the same
    # value, which is the whole reason the phase is exposed at all.
    execution_phase: JobStatus | None = None
    retry_available_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.id <= 0 or self.agent_instance_id <= 0:
            raise InvalidRun
        validate_input_text(self.input_text, self.limits)
        created = _utc(self.created_at)
        started = _utc(self.started_at) if self.started_at else None
        finished = _utc(self.finished_at) if self.finished_at else None
        invalid_start = started is not None and started < created
        # A Run closed by exhausted pre-start recovery has no start boundary, so its finish only
        # has to follow its creation. Every other Run still has to finish after it started.
        invalid_finish = finished is not None and (
            (started is not None and finished < started) or (started is None and finished < created)
        )
        if invalid_start or invalid_finish:
            raise InvalidRun("invalid timestamp order")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "started_at", started)
        object.__setattr__(self, "finished_at", finished)
        if self.elapsed_ms is not None and self.elapsed_ms < 0:
            raise InvalidRun("invalid elapsed time")
        if self.retry_available_at is not None:
            # The instant is only meaningful while the Job is actually waiting to retry; anywhere
            # else it would be a scheduling claim the durable Job does not make.
            if self.execution_phase is not JobStatus.RETRY_WAIT:
                raise InvalidRun("a retry instant requires a waiting phase")
            object.__setattr__(self, "retry_available_at", _utc(self.retry_available_at))
        if self.finish_reason is not None:
            validate_outcome_code(self.finish_reason)
        if self.status is RunStatus.CREATED:
            self._require_empty(started, finished)
        elif self.status is RunStatus.RUNNING:
            if started is None:
                raise InvalidRun
            self._require_empty(None, finished)
        elif self.status is RunStatus.SUCCEEDED:
            if (
                started is None
                or finished is None
                or self.elapsed_ms is None
                or self.output_text is None
            ):
                raise InvalidRun
            validate_output_text(self.output_text, self.limits)
            if self.error_code is not None or self.error_message is not None:
                raise InvalidRun
        elif self.status is RunStatus.CANCELLED:
            self._require_cancellation(started, finished)
        else:
            if finished is None or self.error_code is None or self.error_message is None:
                raise InvalidRun
            validate_outcome_code(self.error_code)
            validate_error_message(self.error_message)
            if self.output_text is not None or self.finish_reason is not None:
                raise InvalidRun
            if self.error_code == WORKER_RECOVERY_EXHAUSTED:
                # The one failed shape that never started. Requiring the empty usage here keeps
                # the domain at least as strict as the lifecycle CHECK.
                if started is not None or self.elapsed_ms is not None:
                    raise InvalidRun("exhausted recovery cannot carry a start boundary")
                if any(value is not None for value in self.usage.values()):
                    raise InvalidRun("exhausted recovery cannot carry usage")
            elif started is None or self.elapsed_ms is None:
                raise InvalidRun

    def _require_cancellation(self, started: datetime | None, finished: datetime | None) -> None:
        """Validate the two legal cancelled shapes, mirroring the lifecycle CHECK exactly.

        Cancellation is a terminal lifecycle, not a provider failure, so it never carries an
        error code or message. A Run cancelled before execution began keeps a NULL start
        boundary and therefore no elapsed time; a Run cancelled after it began keeps its real
        start and carries the truthful interval until cancellation was accepted.
        """
        if finished is None:
            raise InvalidRun("cancelled run requires a finish boundary")
        if self.output_text is not None or self.finish_reason is not None:
            raise InvalidRun("cancelled run cannot carry output")
        if self.error_code is not None or self.error_message is not None:
            raise InvalidRun("cancelled run cannot carry a provider error")
        if any(value is not None for value in self.usage.values()):
            raise InvalidRun("cancelled run cannot carry usage")
        if started is None:
            if self.elapsed_ms is not None:
                raise InvalidRun("cancelled run without a start cannot carry elapsed time")
        elif self.elapsed_ms is None:
            raise InvalidRun("cancelled run with a start requires elapsed time")

    def _require_empty(self, started: datetime | None, finished: datetime | None) -> None:
        if any(
            value is not None
            for value in (
                started,
                finished,
                self.output_text,
                self.finish_reason,
                self.error_code,
                self.error_message,
                self.elapsed_ms,
            )
        ):
            raise InvalidRun
        if any(value is not None for value in self.usage.values()):
            raise InvalidRun


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidRun("timestamp must be aware")
    return value.astimezone(UTC)
