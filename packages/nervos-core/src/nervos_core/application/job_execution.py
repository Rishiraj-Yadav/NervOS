"""Stage C2 durable Job execution: lease, heartbeat, terminalization, and honest retry policy.

This module is the orchestration seam between the durable queue and the provider-neutral
`RunExecutor`. It owns four decisions that deliberately do **not** live in the executor:

* the lease and heartbeat cadence,
* the recorded retry disposition (evidence only — C2 never retries),
* the bounded persistence-finalization retry, and
* the "expired lease means lost authority" rule.

Nothing here is provider-specific: the service receives an already-resolved
`ModelCompletion`, never an SDK type.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, TypeVar

from nervos_core.application.clock import Clock, require_utc
from nervos_core.application.errors import PersistenceContention, PersistenceUnavailable
from nervos_core.application.model_completion import (
    INTERNAL_EXECUTION_ERROR,
    MODEL_ACCOUNT_UNAVAILABLE,
    MODEL_AUTHENTICATION_FAILED,
    MODEL_OUTPUT_INCOMPLETE,
    MODEL_OUTPUT_TOO_LARGE,
    MODEL_PERMISSION_DENIED,
    MODEL_RATE_LIMITED,
    MODEL_REFUSED,
    MODEL_REQUEST_REJECTED,
    MODEL_RESPONSE_INVALID,
    MODEL_TIMED_OUT,
    MODEL_UNAVAILABLE,
    ModelCompletion,
)
from nervos_core.application.run_execution import ExecutionOutcome, RunExecutor
from nervos_core.domain.jobs import RetryDisposition
from nervos_core.domain.runs import ModelUsage, Run

# A renewable liveness window, never a task timeout. Four renewal opportunities per lease
# are what keep a long provider call from losing its own claim.
LEASE_DURATION = timedelta(seconds=60)
HEARTBEAT_INTERVAL = timedelta(seconds=15)

if LEASE_DURATION < 3 * HEARTBEAT_INTERVAL:  # pragma: no cover - import-time invariant
    raise RuntimeError("lease duration must allow at least three heartbeats")

# Bounded persistence-finalization retry. This replays only the terminal transaction; the
# provider is never invoked again.
PERSISTENCE_FINALIZATION_ATTEMPTS = 5
PERSISTENCE_FINALIZATION_BACKOFF_SECONDS = (0.2, 0.4, 0.8, 1.6)

# C2 records evidence; it never acts on it. Every failure terminalizes the Job and Run as
# `failed`, and the transient retry state is never written by this milestone.
DISPOSITION_BY_CODE: Mapping[str, RetryDisposition] = MappingProxyType(
    {
        MODEL_RATE_LIMITED: RetryDisposition.SAFE_TO_RETRY,
        MODEL_TIMED_OUT: RetryDisposition.AMBIGUOUS,
        MODEL_UNAVAILABLE: RetryDisposition.AMBIGUOUS,
        INTERNAL_EXECUTION_ERROR: RetryDisposition.AMBIGUOUS,
        MODEL_AUTHENTICATION_FAILED: RetryDisposition.DO_NOT_RETRY,
        MODEL_PERMISSION_DENIED: RetryDisposition.DO_NOT_RETRY,
        MODEL_ACCOUNT_UNAVAILABLE: RetryDisposition.DO_NOT_RETRY,
        MODEL_REQUEST_REJECTED: RetryDisposition.DO_NOT_RETRY,
        MODEL_REFUSED: RetryDisposition.DO_NOT_RETRY,
        MODEL_RESPONSE_INVALID: RetryDisposition.DO_NOT_RETRY,
        MODEL_OUTPUT_INCOMPLETE: RetryDisposition.DO_NOT_RETRY,
        MODEL_OUTPUT_TOO_LARGE: RetryDisposition.DO_NOT_RETRY,
    }
)


def disposition_for(error_code: str) -> RetryDisposition:
    """Return the recorded disposition for a normalized failure code.

    Fails closed: an unrecognized code is `AMBIGUOUS`, never a claim that replay is safe.
    """
    return DISPOSITION_BY_CODE.get(error_code, RetryDisposition.AMBIGUOUS)


_T = TypeVar("_T")


class ClaimState(StrEnum):
    """The durable state of a claim on the worker's side of a reconciliation read."""

    ACTIVE = "active"
    TERMINAL = "terminal"
    LOST = "lost"


@dataclass(frozen=True, slots=True)
class ClaimedAttempt:
    """The in-memory handle a Worker holds for one claimed Job.

    C1 deliberately has no `active_attempt_id` column, so this object — not a database
    pointer — is how the owning Worker identifies its own attempt. Start, heartbeat, and
    terminalization are all fenced on these values plus an unexpired lease.
    """

    job_id: int
    run_id: int
    attempt_id: int
    attempt_number: int
    worker_id: str
    # The claim token is a bearer capability: anyone holding it can terminalize this Job. It
    # stays in memory as real authority, but it must never reach a log line, a Run Event, an
    # API response, or a traceback, so it is excluded from the generated `repr`.
    claim_token: bytes = field(repr=False)
    lease_expires_at: datetime


class JobExecutionPersistence(Protocol):
    """Durable operations the Worker needs; implemented only outside the control plane."""

    def claim_next(
        self,
        *,
        worker_id: str,
        provider_ids: tuple[str, ...],
        max_active: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedAttempt | None: ...

    def load_run(self, run_id: int) -> Run: ...

    def start_attempt(self, claim: ClaimedAttempt, *, now: datetime) -> bool: ...

    def renew_lease(
        self, claim: ClaimedAttempt, *, now: datetime, lease_duration: timedelta
    ) -> bool: ...

    def inspect_claim(self, claim: ClaimedAttempt, *, now: datetime) -> ClaimState: ...

    def succeed(
        self,
        claim: ClaimedAttempt,
        *,
        output_text: str,
        finish_reason: str | None,
        usage: ModelUsage,
        elapsed_ms: int,
        now: datetime,
    ) -> bool: ...

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
    ) -> bool: ...


class JobExecutionService:
    """Execute one claimed Job end to end without ever re-invoking a model."""

    def __init__(
        self,
        persistence: JobExecutionPersistence,
        executor: RunExecutor,
        completions: Mapping[str, ModelCompletion],
        clock: Clock,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        lease_duration: timedelta = LEASE_DURATION,
        heartbeat_interval: timedelta = HEARTBEAT_INTERVAL,
    ) -> None:
        self._persistence = persistence
        self._executor = executor
        self._completions = completions
        self._clock = clock
        self._sleep = sleep
        self._lease_duration = lease_duration
        self._heartbeat_interval = heartbeat_interval

    async def execute(self, claim: ClaimedAttempt) -> ExecutionOutcome | None:
        """Start, execute, and terminalize one claimed Job.

        Returns the committed outcome, or `None` when the claim never reached execution or
        its authority was lost. In every `None` case the Job and Attempt are left exactly as
        they are for C3 to reconcile — C2 neither fabricates a result nor a failure.
        """
        if not await self._offload(
            self._persistence.start_attempt, claim, now=require_utc(self._clock())
        ):
            # The execution-start boundary did not commit, so the provider is never called.
            return None

        lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(claim, lost))
        try:
            outcome = await self._run_provider(claim, lost)
            if outcome is None:
                return None
            await self._finalize(claim, outcome)
            return outcome
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _run_provider(
        self, claim: ClaimedAttempt, lost: asyncio.Event
    ) -> ExecutionOutcome | None:
        """Run the immutable snapshot, abandoning it the moment authority is lost."""
        run = await self._offload(self._persistence.load_run, claim.run_id)
        completion = self._completions.get(run.model_provider)
        if completion is None:
            # Capability absence is a queue state, not an execution outcome: never fail the
            # Job for it. This is unreachable while claim eligibility filters on the same
            # configured provider set, so it is a defensive strand rather than a policy.
            return None
        execution = asyncio.create_task(self._executor.execute(run, completion))
        watch = asyncio.create_task(lost.wait())
        try:
            await asyncio.wait({execution, watch}, return_when=asyncio.FIRST_COMPLETED)
            if execution.done():
                return execution.result()
            # Authority was lost while the provider was still running: stop waiting for, and
            # never accept, a result we are no longer allowed to persist.
            return None
        finally:
            # Runs on every exit path, including cancellation of the outer orchestration task
            # during shutdown, so no local provider task is ever left detached to finish
            # unsupervised.
            await self._settle(execution, watch)

    async def _settle(self, *tasks: asyncio.Task[object]) -> None:
        """Cancel the given tasks and collect them, leaving none detached.

        Cancelling here stops *NervOS* from waiting for and accepting a result. It does not
        guarantee the remote provider has stopped processing a request it already received:
        NervOS claims no provider rollback and no remote cancellation. Because the tasks are
        gathered with `return_exceptions=True`, an already-finished task's outcome is
        collected rather than re-raised, so this cleanup never masks the exception that
        caused the exit.
        """
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _heartbeat(self, claim: ClaimedAttempt, lost: asyncio.Event) -> None:
        """Renew the lease independently of the provider await.

        It keeps running all the way through finalization, because a lease that lapses while
        the worker is trying to durably commit an already-computed result is a gratuitous way
        to lose work.
        """
        while True:
            await asyncio.sleep(self._heartbeat_interval.total_seconds())
            try:
                renewed = await self._offload(
                    self._persistence.renew_lease,
                    claim,
                    now=require_utc(self._clock()),
                    lease_duration=self._lease_duration,
                )
            except PersistenceUnavailable:
                # A transient local failure is not authority loss; the lease margin is the slack.
                continue
            if renewed:
                continue
            state = await self._offload(
                self._persistence.inspect_claim, claim, now=require_utc(self._clock())
            )
            if state is ClaimState.ACTIVE:  # pragma: no cover - defensive re-read
                continue
            if state is ClaimState.TERMINAL:
                return
            lost.set()
            return

    async def _finalize(self, claim: ClaimedAttempt, outcome: ExecutionOutcome) -> None:
        """Persist the terminal state, retrying only the durable write.

        Every attempt is fully fenced. An unknown persistence failure is never replayed
        blindly: durable state is read back first, and only a read that proves the work is
        still ours and still uncommitted allows another attempt.
        """
        for attempt in range(PERSISTENCE_FINALIZATION_ATTEMPTS):
            try:
                committed: bool | None = await self._offload(self._terminalize, claim, outcome)
            except PersistenceContention:
                committed = None
            except PersistenceUnavailable:
                state = await self._offload(
                    self._persistence.inspect_claim, claim, now=require_utc(self._clock())
                )
                if state is ClaimState.TERMINAL:
                    return  # already durably committed by a write we did not observe
                if state is not ClaimState.ACTIVE:
                    return  # authority lost or state inconsistent: discard and strand
                committed = None  # proven still ours and still uncommitted
            if committed is not None:
                return
            if attempt + 1 < PERSISTENCE_FINALIZATION_ATTEMPTS:
                await self._sleep(PERSISTENCE_FINALIZATION_BACKOFF_SECONDS[attempt])
        # Bounded retries exhausted: leave the Attempt/Job/Run exactly as they are for C3.
        return

    def _terminalize(self, claim: ClaimedAttempt, outcome: ExecutionOutcome) -> bool:
        now = require_utc(self._clock())
        if outcome.status == "succeeded":
            assert outcome.output_text is not None
            return self._persistence.succeed(
                claim,
                output_text=outcome.output_text,
                finish_reason=outcome.finish_reason,
                usage=outcome.usage,
                elapsed_ms=outcome.elapsed_ms,
                now=now,
            )
        assert outcome.error_code is not None and outcome.error_message is not None
        return self._persistence.fail(
            claim,
            error_code=outcome.error_code,
            error_message=outcome.error_message,
            retry_disposition=disposition_for(outcome.error_code),
            usage=outcome.usage,
            elapsed_ms=outcome.elapsed_ms,
            now=now,
        )

    async def _offload(self, function: Callable[..., _T], /, *args: object, **kwargs: object) -> _T:
        """Run one blocking persistence call off the event loop.

        SQLAlchemy's SQLite driver is synchronous and `busy_timeout=5000` means one contended
        write can block its caller for five seconds. Calling that directly from a slot task
        would freeze every other coroutine in the process, including an unrelated Job's
        heartbeat and its own timeout deadline.
        """
        return await asyncio.to_thread(function, *args, **kwargs)
