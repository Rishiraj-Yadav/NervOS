"""Stage C2/C4 durable Job execution: lease, heartbeat, terminalization, and honest retry policy.

This module is the orchestration seam between the durable queue and the provider-neutral
`RunExecutor`. It owns four decisions that deliberately do **not** live in the executor:

* the lease and heartbeat cadence,
* the recorded retry disposition and — since C4 — the activation of the one disposition that
  is positively safe to replay,
* the bounded persistence-finalization retry, and
* the "expired lease means lost authority" rule.

Nothing here is provider-specific: the service receives an already-resolved
`ModelCompletion`, never an SDK type.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
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
    safe_error_message,
)
from nervos_core.application.retry_policy import PRODUCTION_RETRY_POLICY, RetryPolicy
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

# How long a cancelled or superseded local provider task is awaited before it is abandoned.
# Python cannot forcibly terminate a coroutine that suppresses cancellation, so this bound is
# what keeps every wait in a slot finite instead of letting one non-cooperative task hang the
# Worker. Abandoning is safe: the durable fences that already protected the result remain.
LOCAL_TASK_DRAIN_TIMEOUT = timedelta(seconds=5)

logger = logging.getLogger(__name__)

# The frozen classification. Exactly one code is positively safe to replay: a normalized rate
# limit is evidence that the provider declined the request, whereas a timeout, an unavailable
# transport, or an internal failure cannot exclude the possibility that the request was
# accepted and processed. Only `SAFE_TO_RETRY` may schedule a durable retry, and the
# transaction additionally requires the exact code, so widening this map alone cannot widen
# the replay surface.
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
    # Terminal *because the owner cancelled the Run*. It is kept apart from `TERMINAL` because
    # the two demand different local behavior: an ordinary terminal state means somebody else
    # already settled work we had computed, so there is nothing left to stop, whereas a
    # cancellation means we must stop waiting on the provider immediately instead of holding
    # the slot until the execution deadline expires.
    CANCELLED = "cancelled"
    LOST = "lost"


class CancellationOutcome(StrEnum):
    """What durably happened to one owner-authorized cancellation request.

    Cancellation is authoritative and immediate, so this is a durable fact rather than an
    acknowledgement: `CANCELLED` covers both the transition that just committed and an
    idempotent repeat against an already-cancelled Run.
    """

    CANCELLED = "cancelled"
    # The Run had already reached a different terminal lifecycle, which cancellation may not
    # rewrite: a succeeded or failed Run stays exactly as it is.
    NOT_CANCELLABLE = "not_cancellable"
    # Foreign and nonexistent are indistinguishable, so cancellation cannot probe for Runs.
    NOT_FOUND = "not_found"


class FailureOutcome(StrEnum):
    """What durably happened to one provider failure the Worker tried to settle.

    C4 splits failure settlement into a retryable and a terminal shape, so a bare `bool` can no
    longer describe the result honestly: "the write did not commit" and "the retry was
    scheduled" are different facts, and confusing them either strands an Attempt or replays a
    provider call. Persistence reports the durable fact; the service decides whether to retry
    only the *database* transition.
    """

    RETRY_SCHEDULED = "retry_scheduled"
    TERMINAL_FAILED = "terminal_failed"
    # Somebody already durably settled this Attempt (our own unobserved COMMIT, or C3).
    ALREADY_SETTLED = "already_settled"
    # Proven still ours and still uncommitted, so replaying the write cannot double-apply.
    UNSETTLED = "unsettled"
    # Lost authority, or a shape we cannot explain. Fail closed: no write, and no provider call.
    UNRESOLVED = "unresolved"


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
    ) -> FailureOutcome: ...

    def inspect_failure(
        self,
        claim: ClaimedAttempt,
        *,
        error_code: str,
        retry_disposition: RetryDisposition,
        anchor_at: datetime,
        retry_policy: RetryPolicy,
        now: datetime,
    ) -> FailureOutcome: ...

    def persist_attempt_usage(
        self, claim: ClaimedAttempt, *, usage: ModelUsage, now: datetime
    ) -> bool:
        """Record the aggregate usage of a multi-turn Attempt.

        It is on this protocol because it is a durable write the Worker performs against the same
        Job/Attempt rows, fenced the same way. It is *not* the Run's usage: a Run still reports the
        usage of its Attempt at terminalization, exactly as Stage C defined.
        """
        ...


class JobExecutionService:
    """Execute one claimed Job end to end without ever re-invoking a model."""

    def __init__(
        self,
        persistence: JobExecutionPersistence,
        executor: RunExecutor,
        completions: Mapping[str, ModelCompletion],
        clock: Clock,
        *,
        retry_policy: RetryPolicy = PRODUCTION_RETRY_POLICY,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        lease_duration: timedelta = LEASE_DURATION,
        heartbeat_interval: timedelta = HEARTBEAT_INTERVAL,
        drain_timeout: timedelta = LOCAL_TASK_DRAIN_TIMEOUT,
        conversation_projection: Callable[..., object] | None = None,
    ) -> None:
        self._persistence = persistence
        self._executor = executor
        self._completions = completions
        self._clock = clock
        self._retry_policy = retry_policy
        self._sleep = sleep
        self._lease_duration = lease_duration
        self._heartbeat_interval = heartbeat_interval
        self._drain_timeout = drain_timeout
        self._conversation_projection = conversation_projection
        # Local tasks this Worker stopped waiting for but could not terminate. They are held
        # only so their eventual result or exception is consumed rather than reported as an
        # unretrieved task exception; they carry no authority to persist anything.
        self._abandoned: set[asyncio.Task[object]] = set()

    @property
    def abandoned_tasks(self) -> frozenset[asyncio.Task[object]]:
        """Return the provider tasks this Worker stopped waiting for and has not yet released.

        Exposed for observability and tests: a task listed here is still running somewhere, and
        holds no authority to persist anything. It is removed as soon as it finishes.
        """
        return frozenset(self._abandoned)

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
            # The scheduling anchor is captured exactly once, the moment a normalized result
            # exists, and is stable across every later database-only replay of the same
            # transition. Recomputing it per attempt would let a BUSY retry drift the durable
            # due time and would leave an uncertain COMMIT with no single expected value to
            # reconcile against.
            anchor_at = require_utc(self._clock())
            await self._finalize(claim, outcome, anchor_at)
            return outcome
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _run_provider(
        self, claim: ClaimedAttempt, lost: asyncio.Event
    ) -> ExecutionOutcome | None:
        """Run the immutable snapshot under three local racers, and let exactly one win.

        The racers are the provider call, the authority-loss watcher, and the Attempt's own
        execution deadline. The deadline is owned here, outside `RunExecutor`, so it remains
        authoritative even when the provider coroutine refuses to cooperate with cancellation:
        the executor's inner `asyncio.timeout` cannot complete against a coroutine that
        suppresses `CancelledError`, and an unanswerable inner bound would otherwise let one
        task hold its slot forever.
        """
        run = await self._offload(self._persistence.load_run, claim.run_id)
        completion = self._completions.get(run.model_provider)
        if completion is None:
            # Capability absence is a queue state, not an execution outcome: never fail the
            # Job for it. This is unreachable while claim eligibility filters on the same
            # configured provider set, so it is a defensive strand rather than a policy.
            return None
        execution = asyncio.create_task(self._executor.execute(run, completion, claim))
        watch = asyncio.create_task(lost.wait())
        # The clock starts here, immediately around the provider invocation and after the
        # durable execution-start commit, so queue time, claim time, a scheduled retry wait,
        # and terminal persistence are all excluded. Each retry Attempt gets a fresh window
        # because this method is re-entered for each one.
        deadline = asyncio.create_task(asyncio.sleep(run.limits.provider_timeout_ms / 1000))
        try:
            await asyncio.wait({execution, watch, deadline}, return_when=asyncio.FIRST_COMPLETED)
            # A result already in hand always wins. Checking it first is what stops a deadline
            # that merely became ready at the same moment from relabelling a completed call as
            # a timeout: the outcome is decided by what already happened, never by which task
            # the loop happened to schedule first.
            if execution.done():
                return execution.result()
            if watch.done():
                # Authority was lost while the provider was still running: stop waiting for,
                # and never accept, a result we are no longer allowed to persist.
                return None
            # The deadline is the only remaining winner.
            return self._timed_out(run)
        finally:
            # Runs on every exit path, including cancellation of the outer orchestration task
            # during shutdown. The wait is bounded; see `_settle` for why a non-cooperative
            # task is abandoned rather than awaited forever.
            await self._settle(execution, watch, deadline)

    def _timed_out(self, run: Run) -> ExecutionOutcome:
        """Return the normalized deadline outcome: ambiguous, and therefore never replayed.

        A local deadline proves NervOS stopped waiting. It cannot prove the provider never
        executed the request it already received, so this is `AMBIGUOUS` and can never satisfy
        C4's retry predicate, which admits only a positively safe normalized failure.
        """
        return ExecutionOutcome(
            status="failed",
            output_text=None,
            finish_reason=None,
            usage=ModelUsage(),
            elapsed_ms=run.limits.provider_timeout_ms,
            error_code=MODEL_TIMED_OUT,
            error_message=safe_error_message(MODEL_TIMED_OUT),
        )

    async def _settle(self, *tasks: asyncio.Task[object]) -> None:
        """Cancel the given tasks and collect them within a bounded wait.

        Cancelling here stops *NervOS* from waiting for and accepting a result. It does not
        guarantee the remote provider has stopped processing a request it already received:
        NervOS claims no provider rollback and no remote cancellation.

        The collection is deliberately bounded. A coroutine that suppresses `CancelledError` or
        blocks in cleanup cannot be forcibly terminated by `asyncio`, and the previous unbounded
        `gather` let exactly that coroutine hang its slot -- and, through the Worker's own drain,
        shutdown itself. Waiting no longer than `drain_timeout` keeps every wait here finite.

        The promise is therefore **no unbounded wait and no durable resurrection**, not the
        stronger claim that every coroutine is forcibly terminated. A task that outlives the
        bound is handed to `_abandon`, which keeps it observable until it finishes and consumes
        its result or exception, and it can never persist anything: this method is only reached
        after the execution-start boundary committed, so the Attempt is either still fenced by a
        lease we hold or already revoked by a cancellation.
        """
        for task in tasks:
            task.cancel()
        done, pending = await asyncio.wait(tasks, timeout=self._drain_timeout.total_seconds())
        # Collect every settled outcome so an already-finished task neither leaks a
        # "never retrieved" warning nor masks the exception that caused this exit.
        for task in done:
            with contextlib.suppress(BaseException):
                task.exception()
        if pending:
            logger.warning("provider_task_abandoned count=%s", len(pending))
            self._abandon(pending)

    def _abandon(self, tasks: Iterable[asyncio.Task[object]]) -> None:
        """Track tasks that outlived the drain bound until they finish on their own."""
        for task in tasks:
            self._abandoned.add(task)
            task.add_done_callback(self._discard_abandoned)

    def _discard_abandoned(self, task: asyncio.Task[object]) -> None:
        """Consume an abandoned task's outcome and stop tracking it.

        Retrieving the exception is what prevents `Task exception was never retrieved`; the
        outcome itself is deliberately thrown away, because a task that lost the race has no
        authority to settle anything.
        """
        self._abandoned.discard(task)
        with contextlib.suppress(BaseException):
            task.exception()

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
            if state is ClaimState.CANCELLED:
                # The owner cancelled this Run, so this Worker must stop waiting on the
                # provider now rather than hold the slot until the execution deadline lapses.
                # The cancellation write deliberately does not wait for this discovery: the
                # durable authority is already revoked, so whatever the provider eventually
                # returns can never be persisted.
                lost.set()
                return
            if state is ClaimState.TERMINAL:
                return
            lost.set()
            return

    async def _finalize(
        self, claim: ClaimedAttempt, outcome: ExecutionOutcome, anchor_at: datetime
    ) -> None:
        """Persist the committed outcome, retrying only the durable write.

        Every attempt is fully fenced. An unknown persistence failure is never replayed
        blindly: durable state is read back first, and only a read that proves the work is
        still ours and still uncommitted allows another attempt.
        """
        if outcome.status == "succeeded":
            await self._finalize_success(claim, outcome)
            return
        await self._finalize_failure(claim, outcome, anchor_at)

    async def _finalize_success(self, claim: ClaimedAttempt, outcome: ExecutionOutcome) -> None:
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
                if self._conversation_projection is not None:
                    with contextlib.suppress(Exception):
                        await self._offload(
                            self._conversation_projection,
                            run_id=claim.run_id,
                            status="succeeded",
                            output_text=outcome.output_text,
                            error_code=None,
                        )
                return
            if attempt + 1 < PERSISTENCE_FINALIZATION_ATTEMPTS:
                await self._sleep(PERSISTENCE_FINALIZATION_BACKOFF_SECONDS[attempt])
        # Bounded retries exhausted: leave the Attempt/Job/Run exactly as they are for C3.
        return

    async def _finalize_failure(
        self, claim: ClaimedAttempt, outcome: ExecutionOutcome, anchor_at: datetime
    ) -> None:
        """Settle one normalized provider failure, retrying only the durable transition.

        The provider has already been invoked exactly once for this Attempt, so every path here
        replays a *database* transition at most; none of them can re-issue the request. A
        retryable failure is only retryable while this Worker still owns a live claim and the
        shared budget is unspent, and both facts are re-read inside the write transaction
        rather than trusted from an earlier application-level read.
        """
        assert outcome.error_code is not None and outcome.error_message is not None
        retry_disposition = disposition_for(outcome.error_code)
        for attempt in range(PERSISTENCE_FINALIZATION_ATTEMPTS):
            try:
                settled: FailureOutcome | None = await self._offload(
                    self._persistence.record_failure,
                    claim,
                    error_code=outcome.error_code,
                    error_message=outcome.error_message,
                    retry_disposition=retry_disposition,
                    usage=outcome.usage,
                    elapsed_ms=outcome.elapsed_ms,
                    anchor_at=anchor_at,
                    retry_policy=self._retry_policy,
                    now=require_utc(self._clock()),
                )
            except PersistenceContention:
                settled = None
            except PersistenceUnavailable:
                settled = await self._offload(
                    self._persistence.inspect_failure,
                    claim,
                    error_code=outcome.error_code,
                    retry_disposition=retry_disposition,
                    anchor_at=anchor_at,
                    retry_policy=self._retry_policy,
                    now=require_utc(self._clock()),
                )
            if settled is FailureOutcome.UNSETTLED or settled is None:
                if attempt + 1 < PERSISTENCE_FINALIZATION_ATTEMPTS:
                    await self._sleep(PERSISTENCE_FINALIZATION_BACKOFF_SECONDS[attempt])
                continue
            if self._conversation_projection is not None:
                with contextlib.suppress(Exception):
                    await self._offload(
                        self._conversation_projection,
                        run_id=claim.run_id,
                        status="failed",
                        output_text=None,
                        error_code=outcome.error_code,
                    )
            return
        # Bounded retries exhausted, or an unexplainable durable shape: strand exactly as C2
        # does, leaving the rows for C3 rather than fabricating an outcome.
        return

    def _terminalize(self, claim: ClaimedAttempt, outcome: ExecutionOutcome) -> bool:
        assert outcome.status == "succeeded" and outcome.output_text is not None
        return self._persistence.succeed(
            claim,
            output_text=outcome.output_text,
            finish_reason=outcome.finish_reason,
            usage=outcome.usage,
            elapsed_ms=outcome.elapsed_ms,
            now=require_utc(self._clock()),
        )

    async def _offload(self, function: Callable[..., _T], /, *args: object, **kwargs: object) -> _T:
        """Run one blocking persistence call off the event loop.

        SQLAlchemy's SQLite driver is synchronous and `busy_timeout=5000` means one contended
        write can block its caller for five seconds. Calling that directly from a slot task
        would freeze every other coroutine in the process, including an unrelated Job's
        heartbeat and its own timeout deadline.
        """
        return await asyncio.to_thread(function, *args, **kwargs)
