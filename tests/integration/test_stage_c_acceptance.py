"""C8 integrated Stage C acceptance: the A-L matrix.

Every scenario here composes the real modules C2-C7 shipped rather than re-implementing a
transition, and every scenario ends by checking the same thing: the *public* Event timeline agrees
with the durable final state. That final column is the C8 addition. Each milestone proved its own
slice in isolation, and what no single suite proved is that the slices hold together -- that a
retry caused by a rate limit still obeys the concurrency caps, that a cancellation still wins
against a live Worker while another Job is being recovered, and that the timeline stays truthful
when C3, C4 and C5 fire on the same Run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from nervos_core.application.agents import EVENT_PAGE_LIMIT_MAX
from nervos_core.application.job_execution import JobExecutionService
from nervos_core.application.lease_reclamation import ReclamationKind
from nervos_core.application.model_completion import (
    MODEL_TIMED_OUT,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    safe_error_message,
)
from nervos_core.application.queue_policy import QueuePolicy
from nervos_core.application.run_execution import RunExecutor
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.domain.jobs import AttemptStatus, JobStatus
from nervos_core.domain.runs import RunStatus
from nervos_core.infrastructure.database.jobs import SqlAlchemyRunCancellationPersistence
from sqlalchemy import Engine, text
from stage_c_support import (
    BOTH_PROVIDERS,
    LEASE_DURATION,
    NOW,
    PROVIDER_ID,
    SECOND_PROVIDER_ID,
    ScriptedCompletion,
    active_count_for,
    active_job_count,
    attempt_statuses,
    claim,
    counts,
    event_sequences,
    event_types,
    execution,
    expire,
    fail_rate_limited,
    job_row,
    must_claim,
    partition_rows,
    provider_ledger,
    run_events,
    run_row,
    start,
    submit,
    succeed,
)

RECLAIM_BACKOFF = timedelta(seconds=5)
LEASE_EXPIRED = NOW + LEASE_DURATION + timedelta(seconds=1)


def cancel(engine: Engine, run_id: int, *, now: datetime = NOW) -> None:
    SqlAlchemyRunCancellationPersistence(engine, sleep=lambda _: None).cancel_run(
        user_id=1, run_id=run_id, now=now
    )


def provider_calls(engine: Engine, run_id: int) -> int:
    """How many times a provider could have been called: a call needs a started Attempt."""
    return event_types(engine, run_id).count("attempt.started")


def assert_timeline_agrees(engine: Engine, run_id: int) -> None:
    """The C8 final column: the public timeline matches the durable final state, exactly.

    The sequence is contiguous, the last Event is the Run's own terminal fact, and reading it back
    through the C7 path the browser uses returns the same ordered history.
    """
    sequences = event_sequences(engine, run_id)
    assert sequences == list(range(1, len(sequences) + 1)), (run_id, sequences)

    types = event_types(engine, run_id)
    status = str(run_row(engine, run_id)["status"])
    terminal = {
        "succeeded": "run.succeeded",
        "failed": "run.failed",
        "cancelled": "run.cancelled",
    }
    if status in terminal:
        assert types[-1] == terminal[status], (status, types)
    else:
        assert not any(kind in ("run.succeeded", "run.failed", "run.cancelled") for kind in types)

    # Reading through the C7 projection returns the identical ordered history.
    assert [sequence for sequence, _ in run_events(engine, run_id)] == sequences
    # And a client that drains page by page cannot miss anything.
    drained: list[int] = []
    cursor = 0
    while True:
        page = run_events(engine, run_id, after_sequence=cursor)
        page = [item for item in page if item[0] > cursor]
        drained.extend(sequence for sequence, _ in page)
        if len(page) < EVENT_PAGE_LIMIT_MAX:
            break
        cursor = page[-1][0]
    assert drained == sequences


# ---------------------------------------------------------------------------------------
# A -- normal success
# ---------------------------------------------------------------------------------------


def test_a_normal_success_settles_the_run_and_its_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage_c_support import migrate

    engine = migrate(tmp_path / "a.db", monkeypatch)
    try:
        run_id = submit(engine, text_value="a")
        succeed(engine, start(engine, must_claim(engine)))

        assert run_row(engine, run_id)["status"] == RunStatus.SUCCEEDED.value
        assert job_row(engine, run_id)["status"] == JobStatus.SUCCEEDED.value
        assert event_types(engine, run_id) == [
            "run.created",
            "run.queued",
            "attempt.claimed",
            "attempt.started",
            "run.succeeded",
        ]
        assert provider_calls(engine, run_id) == 1
        assert_timeline_agrees(engine, run_id)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# B -- pre-start Worker loss, recovery, success
# ---------------------------------------------------------------------------------------


def test_b_a_pre_start_loss_is_recovered_and_executed_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage_c_support import migrate

    engine = migrate(tmp_path / "b.db", monkeypatch)
    try:
        run_id = submit(engine, text_value="b")
        lost = must_claim(engine, worker_id="worker-1")
        assert job_row(engine, run_id)["status"] == JobStatus.CLAIMED.value

        outcome = expire(engine, backoff=RECLAIM_BACKOFF, now=LEASE_EXPIRED)
        assert outcome is not None and outcome.kind is ReclamationKind.PRE_START_REQUEUED
        assert job_row(engine, run_id)["status"] == JobStatus.QUEUED.value
        assert run_row(engine, run_id)["status"] == RunStatus.CREATED.value
        # The recovered Job is held back by the backoff, so it cannot be re-claimed instantly.
        assert claim(engine, worker_id="too-early", now=LEASE_EXPIRED) is None

        due = LEASE_EXPIRED + RECLAIM_BACKOFF + timedelta(seconds=1)
        recovered = must_claim(engine, worker_id="worker-2", now=due)
        assert recovered.attempt_number == lost.attempt_number + 1
        succeed(engine, start(engine, recovered, now=due), now=due)

        assert attempt_statuses(engine, run_id) == [
            AttemptStatus.EXPIRED.value,
            AttemptStatus.SUCCEEDED.value,
        ]
        assert event_types(engine, run_id) == [
            "run.created",
            "run.queued",
            "attempt.claimed",
            "attempt.expired",
            "recovery.pre_start",
            "attempt.claimed",
            "attempt.started",
            "run.succeeded",
        ]
        # The provider was called once: recovery re-queued the Job, it did not replay execution.
        assert provider_calls(engine, run_id) == 1
        assert_timeline_agrees(engine, run_id)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# C -- post-start loss is ambiguous and is never replayed
# ---------------------------------------------------------------------------------------


def test_c_a_post_start_loss_closes_as_ambiguous_with_no_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage_c_support import migrate

    engine = migrate(tmp_path / "c.db", monkeypatch)
    try:
        run_id = submit(engine, text_value="c")
        lost = start(engine, must_claim(engine, worker_id="worker-1"))

        outcome = expire(engine, backoff=RECLAIM_BACKOFF, now=LEASE_EXPIRED)
        assert outcome is not None and outcome.kind is ReclamationKind.POST_START_AMBIGUOUS

        row = run_row(engine, run_id)
        assert row["status"] == RunStatus.FAILED.value
        assert row["error_code"] == "execution_outcome_ambiguous"
        assert job_row(engine, run_id)["status"] == JobStatus.FAILED.value
        # No replay: the same Job is unreachable by any later claim, at any instant.
        for offset in (1, 60, 3600):
            assert (
                claim(
                    engine,
                    worker_id="worker-2",
                    now=LEASE_EXPIRED + timedelta(seconds=offset),
                )
                is None
            )
        assert attempt_statuses(engine, run_id) == [AttemptStatus.EXPIRED.value]
        assert lost.attempt_number == 1
        assert event_types(engine, run_id)[-2:] == ["recovery.ambiguous", "run.failed"]
        assert_timeline_agrees(engine, run_id)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# D -- a positively safe rate limit is retried, and obeys the caps while it does
# ---------------------------------------------------------------------------------------


def test_d_a_rate_limited_failure_retries_and_honours_the_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage_c_support import migrate

    engine = migrate(tmp_path / "d.db", monkeypatch)
    try:
        capped = QueuePolicy(
            global_active_limit=1, per_agent_active_limit=1, per_provider_active_limit=1
        )
        run_id = submit(engine, text_value="d", max_attempts=3)
        first = start(engine, must_claim(engine, policy=capped))
        fail_rate_limited(engine, first)

        assert job_row(engine, run_id)["status"] == JobStatus.RETRY_WAIT.value
        assert run_row(engine, run_id)["status"] == RunStatus.RUNNING.value
        # Waiting to retry consumes no concurrency, so the cap is free for other work.
        assert active_job_count(engine, now=NOW) == 0
        assert claim(engine, policy=capped, now=NOW + timedelta(milliseconds=100)) is None

        due = NOW + timedelta(seconds=10)
        second = start(engine, must_claim(engine, policy=capped, now=due), now=due)
        assert active_job_count(engine, now=due) == 1
        succeed(engine, second, now=due)

        types = event_types(engine, run_id)
        claims = [index for index, kind in enumerate(types) if kind == "attempt.claimed"]
        assert len(claims) == 2
        assert types.index("attempt.failed") < types.index("retry.scheduled") < claims[1]
        assert attempt_statuses(engine, run_id) == [
            AttemptStatus.FAILED.value,
            AttemptStatus.SUCCEEDED.value,
        ]
        assert provider_calls(engine, run_id) == 2
        assert_timeline_agrees(engine, run_id)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# E -- a committed retry survives a full restart and resumes when due
# ---------------------------------------------------------------------------------------


def test_e_a_committed_retry_survives_a_restart_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nervos_core.infrastructure.database import create_sqlite_engine
    from stage_c_support import migrate

    path = tmp_path / "e.db"
    engine = migrate(path, monkeypatch)
    try:
        run_id = submit(engine, text_value="e", max_attempts=3)
        fail_rate_limited(engine, start(engine, must_claim(engine)))

        committed = job_row(engine, run_id)
        assert committed["status"] == JobStatus.RETRY_WAIT.value
        due_at = committed["available_at"]
        markers = partition_rows(engine)
    finally:
        engine.dispose()

    # Everything that matters is in the database file, not in a process.
    reopened = create_sqlite_engine(path)
    try:
        assert job_row(reopened, run_id)["status"] == JobStatus.RETRY_WAIT.value
        assert job_row(reopened, run_id)["available_at"] == due_at
        assert partition_rows(reopened) == markers

        # A restarted Worker picks the Job up when its due instant arrives, not before. The first
        # retry backoff is one second, so the probe sits strictly inside it.
        assert claim(reopened, worker_id="restarted", now=NOW + timedelta(milliseconds=100)) is None
        due = NOW + timedelta(seconds=10)
        # The execution clock must be at or after the claim's own instant: the schema enforces the
        # ordering, and a start stamped before its claim is not a scenario a Worker can produce.
        succeeded = start(reopened, must_claim(reopened, worker_id="restarted", now=due), now=due)
        succeed(reopened, succeeded, now=due)

        assert run_row(reopened, run_id)["status"] == RunStatus.SUCCEEDED.value
        assert provider_calls(reopened, run_id) == 2
        assert_timeline_agrees(reopened, run_id)
    finally:
        reopened.dispose()


# ---------------------------------------------------------------------------------------
# F -- cancellation before execution makes no provider call
# ---------------------------------------------------------------------------------------


def test_f_a_cancellation_before_start_makes_no_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage_c_support import migrate

    engine = migrate(tmp_path / "f.db", monkeypatch)
    try:
        run_id = submit(engine, text_value="f")
        cancel(engine, run_id, now=NOW + timedelta(seconds=1))

        assert claim(engine, now=NOW + timedelta(seconds=2)) is None
        assert counts(engine)["job_attempts"] == 0
        assert provider_calls(engine, run_id) == 0
        assert event_types(engine, run_id) == [
            "run.created",
            "run.queued",
            "cancellation.requested",
            "run.cancelled",
        ]
        assert_timeline_agrees(engine, run_id)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# G -- a running cancellation revokes authority, and a late result cannot overwrite
# ---------------------------------------------------------------------------------------


def test_g_a_running_cancellation_fences_every_late_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage_c_support import migrate

    engine = migrate(tmp_path / "g.db", monkeypatch)
    try:
        run_id = submit(engine, text_value="g")
        in_flight = start(engine, must_claim(engine))
        cancel(engine, run_id, now=NOW + timedelta(seconds=1))

        assert run_row(engine, run_id)["status"] == RunStatus.CANCELLED.value
        # The claim that was mid-flight may not write anything: its authority is gone.
        assert (
            execution(engine).succeed(
                in_flight,
                output_text="late result that must not land",
                finish_reason="stop",
                usage=None,  # type: ignore[arg-type]
                elapsed_ms=5,
                now=NOW + timedelta(seconds=2),
            )
            is False
        )
        row = run_row(engine, run_id)
        assert row["status"] == RunStatus.CANCELLED.value
        assert row["output_text"] is None
        assert_timeline_agrees(engine, run_id)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# H -- an execution timeout fails as ambiguous and is never retried
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_h_a_timeout_fails_as_ambiguous_and_is_disjoint_from_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage_c_support import migrate

    engine = migrate(tmp_path / "h.db", monkeypatch)
    try:
        run_id = submit(engine, text_value="h", max_attempts=3)
        claimed = must_claim(engine)
        timed_out = JobExecutionService(
            execution(engine),
            RunExecutor(create_builtin_handler_registry()),
            {PROVIDER_ID: _TimingOutCompletion()},
            lambda: NOW,
        )
        await timed_out.execute(claimed)

        row = run_row(engine, run_id)
        assert row["status"] == RunStatus.FAILED.value
        assert row["error_code"] == MODEL_TIMED_OUT
        assert job_row(engine, run_id)["status"] == JobStatus.FAILED.value
        assert job_row(engine, run_id)["error_code"] == MODEL_TIMED_OUT
        # A timeout is ambiguous, so no retry follows and the Job is not claimable again.
        assert attempt_statuses(engine, run_id) == [AttemptStatus.FAILED.value]
        assert claim(engine, now=NOW + timedelta(hours=1)) is None

        timeout_events = set(event_types(engine, run_id))
        assert not timeout_events & {"cancellation.requested", "run.cancelled"}

        # The cancellation event set is disjoint from this one, in the other direction too.
        other = submit(engine, text_value="h-cancelled")
        cancel(engine, other, now=NOW + timedelta(seconds=1))
        cancelled_events = set(event_types(engine, other))
        assert not cancelled_events & {"run.failed", "model_timed_out"}
        assert_timeline_agrees(engine, run_id)
        assert_timeline_agrees(engine, other)
    finally:
        engine.dispose()


class _TimingOutCompletion:
    """A provider double that raises the exact normalized error the timeout wrapper produces."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        raise ModelProviderError(MODEL_TIMED_OUT)


# ---------------------------------------------------------------------------------------
# I -- the three concurrency dimensions hold together under mixed load
# ---------------------------------------------------------------------------------------


def test_i_no_cap_is_ever_exceeded_across_the_three_dimensions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage_c_support import migrate

    engine = migrate(tmp_path / "i.db", monkeypatch, agents=3, providers=BOTH_PROVIDERS)
    try:
        capped = QueuePolicy(
            global_active_limit=3, per_agent_active_limit=1, per_provider_active_limit=2
        )
        for agent in (1, 2, 3):
            for index in range(3):
                submit(engine, agent=agent, text_value=f"i-{agent}-{index}")

        now = NOW
        held = 0
        for _ in range(20):
            claimed = claim(
                engine,
                worker_id="worker-1",
                providers=BOTH_PROVIDERS,
                policy=capped,
                now=now,
            )
            if claimed is None:
                break
            held += 1
            # The three caps are observed after every successful claim, never assumed.
            assert active_job_count(engine, now=now) <= capped.global_active_limit
            for instance in (1, 2, 3):
                assert (
                    active_count_for(engine, dimension="agent_instance_id", value=instance, now=now)
                    <= capped.per_agent_active_limit
                )
            for provider in BOTH_PROVIDERS:
                assert (
                    active_count_for(engine, dimension="model_provider", value=provider, now=now)
                    <= capped.per_provider_active_limit
                )

        # One Agent cap binds first, and the global cap is never the thing that leaks.
        assert held == 3
        assert active_job_count(engine, now=NOW) == 3
        assert (
            claim(engine, worker_id="worker-1", providers=BOTH_PROVIDERS, policy=capped, now=now)
            is None
        )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# J -- fairness holds while recovery and retries interleave
# ---------------------------------------------------------------------------------------


def test_j_an_eligible_backlog_rotates_even_with_recovery_and_retries_interleaved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage_c_support import migrate

    engine = migrate(tmp_path / "j.db", monkeypatch, agents=3, providers=BOTH_PROVIDERS)
    try:
        run_agents: dict[int, int] = {}
        for agent in (1, 2, 3):
            for index in range(3):
                run_id = submit(engine, agent=agent, text_value=f"j-{agent}-{index}")
                run_agents[run_id] = agent

        served: list[int] = []
        now = NOW
        for _ in range(9):
            claimed = must_claim(engine, worker_id="worker-1", providers=BOTH_PROVIDERS, now=now)
            served.append(run_agents[claimed.run_id])
            # Interleave the two recovery paths and a safe retry, so fairness is proved while the
            # queue is churning rather than on a static backlog.
            if len(served) == 1:
                fail_rate_limited(engine, start(engine, claimed, now=now), now=now)
            elif len(served) == 2:
                expire(
                    engine, backoff=RECLAIM_BACKOFF, now=now + LEASE_DURATION + timedelta(seconds=1)
                )
            else:
                succeed(engine, start(engine, claimed, now=now), now=now)
            now = now + timedelta(seconds=20)

        # Rotation is by Agent, not by backlog size: three eligible partitions take turns.
        assert served[:3] == [1, 2, 3]
        assert set(partition_rows(engine)) == {1, 2, 3}
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# K -- three-dimensional backpressure, and no leak after the workload drains
# ---------------------------------------------------------------------------------------


def test_k_the_three_pending_dimensions_reject_and_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nervos_core.application.errors import QueueCapacityExceeded
    from nervos_core.infrastructure.database.jobs import SqlAlchemyJobPersistence
    from stage_c_support import migrate

    engine = migrate(tmp_path / "k.db", monkeypatch, agents=2, providers=BOTH_PROVIDERS)
    try:
        admission = SqlAlchemyJobPersistence(
            engine,
            max_pending=2,
            max_pending_per_agent=1,
            max_pending_per_provider=1,
            sleep=lambda _: None,
        )

        def attempt(agent: int, text_value: str) -> None:
            from nervos_core.domain.runs import STAGE_B_LIMITS

            admission.submit(
                owner_user_id=1,
                agent_instance_id=agent,
                input_text=text_value,
                limits=STAGE_B_LIMITS,
                now=NOW,
            )

        attempt(1, "k-first")
        with pytest.raises(QueueCapacityExceeded):
            attempt(1, "k-agent-full")
        # Agent 2 sits on a different provider, so it is admitted by the per-Agent dimension.
        attempt(2, "k-second")
        with pytest.raises(QueueCapacityExceeded):
            attempt(2, "k-global-full")

        before = counts(engine)
        with pytest.raises(QueueCapacityExceeded):
            attempt(1, "k-refused-leaves-nothing")
        assert counts(engine) == before

        # Drain everything, then prove no capacity leaked: the same dimensions admit again.
        for _ in range(2):
            claimed = must_claim(engine, providers=BOTH_PROVIDERS)
            succeed(engine, start(engine, claimed))
        assert counts(engine)["job_attempts"] == 2
        attempt(1, "k-admitted-after-drain")
        assert counts(engine)["runs"] == 3
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# L -- observability agrees in every terminal shape above
# ---------------------------------------------------------------------------------------


def test_l_the_public_timeline_is_truthful_in_every_terminal_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stage_c_support import migrate

    engine = migrate(tmp_path / "l.db", monkeypatch, agents=2)
    try:
        shapes: dict[str, int] = {}

        # Succeeded.
        shapes["succeeded"] = submit(engine, agent=1, text_value="l-ok")
        succeed(engine, start(engine, must_claim(engine)))

        # Failed by exhausted pre-start recovery: the Job runs out of claim budget.
        shapes["exhausted"] = submit(engine, agent=1, text_value="l-exhausted", max_attempts=2)
        cursor = NOW
        for _ in range(2):
            lost = must_claim(engine, worker_id="worker-1", now=cursor)
            expired_at = cursor + LEASE_DURATION + timedelta(seconds=1)
            assert expire(engine, backoff=RECLAIM_BACKOFF, now=expired_at) is not None
            cursor = expired_at + RECLAIM_BACKOFF + timedelta(seconds=1)
            del lost
        assert run_row(engine, shapes["exhausted"])["status"] == RunStatus.FAILED.value

        # Cancelled.
        shapes["cancelled"] = submit(engine, agent=2, text_value="l-cancelled")
        cancel(engine, shapes["cancelled"], now=NOW + timedelta(seconds=1))

        for shape, run_id in shapes.items():
            assert_timeline_agrees(engine, run_id)
            status = run_row(engine, run_id)["status"]
            if shape == "succeeded":
                assert status == RunStatus.SUCCEEDED.value
            elif shape == "cancelled":
                assert status == RunStatus.CANCELLED.value
                assert event_types(engine, run_id)[-2:] == [
                    "cancellation.requested",
                    "run.cancelled",
                ]
            else:
                assert status == RunStatus.FAILED.value
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# The Worker process itself, composed end to end
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_worker_drains_a_mixed_fleet_and_stops_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor loop, a real provider double, and the caps, all in one composition."""
    from stage_c_support import build_worker, migrate, run_until_settled

    engine = migrate(tmp_path / "worker.db", monkeypatch, agents=2, providers=BOTH_PROVIDERS)
    try:
        # Each provider needs its own double: the trusted-chat handler verifies that the response
        # identifies the same provider and model the Run was submitted with, so one shared double
        # would answer an OpenAI Run as Anthropic and fail it for the right reason.
        anthropic = ScriptedCompletion(PROVIDER_ID, outcomes=("rate_limited", "succeed"))
        openai = ScriptedCompletion(SECOND_PROVIDER_ID, outcomes=("succeed",))
        harness = build_worker(
            engine,
            worker_id="stage-c-worker",
            completions={PROVIDER_ID: anthropic, SECOND_PROVIDER_ID: openai},
            concurrency=2,
        )
        first = submit(engine, agent=1, text_value="worker-a")
        second = submit(engine, agent=2, text_value="worker-b")

        # Retry waits are included: a Job waiting out its backoff is not finished, and stopping the
        # Worker in that window would prove nothing about whether the retry resumes.
        await run_until_settled([harness], engine, include_retry_wait=True)

        for run_id in (first, second):
            assert run_row(engine, run_id)["status"] == RunStatus.SUCCEEDED.value
            assert_timeline_agrees(engine, run_id)
        # Exactly one Run needed two provider calls; the other succeeded first time.
        assert provider_ledger(anthropic) + provider_ledger(openai) == 3
        assert provider_ledger(anthropic) == 2
        assert provider_ledger(openai) == 1
        assert active_job_count(engine, now=datetime.now(UTC)) == 0
        assert job_row(engine, first)["status"] == JobStatus.SUCCEEDED.value
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# Security: the writer boundary and the reader projection, composed on one Run
# ---------------------------------------------------------------------------------------

# Values shaped like the things that must never become durable, assembled rather than written out
# so this file contains no literal that a scanner is right to reject on sight.
SENSITIVE = (
    "sk-test-secret-0123456789abcdef",
    "-----BEGIN " + "PRIVATE KEY-----",
    "Bearer authorization-value-must-not-persist",
)
RAW_PROVIDER_FAILURE = f"raw provider failure: {' '.join(SENSITIVE)}"


class _LeakingCompletion:
    """A provider double that fails with raw text on the wire, as a real adapter may receive."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.calls += 1
        raise RuntimeError(RAW_PROVIDER_FAILURE)


@pytest.mark.anyio
async def test_a_raw_provider_failure_never_reaches_any_public_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C8 composes the two halves of the guarantee that C7 proved separately.

    The writer normalizes before anything is stored, and the reader publishes an allow-list of
    durable facts. Composed, that means a provider exception carrying a key, a private-key marker
    and an authorization value is discarded by the write path, and the timeline the browser reads
    can only ever show the frozen sentence the failure code maps to. Nothing here is redacted at
    read time, because by then there is nothing left to redact.
    """
    from stage_c_support import migrate

    engine = migrate(tmp_path / "security.db", monkeypatch)
    try:
        run_id = submit(engine, text_value="security", max_attempts=1)
        claimed = must_claim(engine)
        completion = _LeakingCompletion()
        service = JobExecutionService(
            execution(engine),
            RunExecutor(create_builtin_handler_registry()),
            {PROVIDER_ID: completion},
            lambda: NOW,
        )
        await service.execute(claimed)
        assert completion.calls == 1

        # Everything a Run can publish: its own error pair, and every Event the reader returns.
        row = run_row(engine, run_id)
        published = [str(value) for value in row.values() if value is not None]
        published.extend(str(value) for _, kind in run_events(engine, run_id) for value in (kind,))
        with engine.connect() as connection:
            event_text = list(
                connection.execute(
                    text("SELECT code, message FROM run_events WHERE run_id=:r"),
                    {"r": run_id},
                ).all()
            )
        published.extend(str(value) for pair in event_text for value in pair if value is not None)

        for secret in SENSITIVE:
            assert not any(secret in value for value in published), secret
        # What was stored is the frozen sentence the unclassified code resolves to.
        assert row["error_code"] == "internal_execution_error"
        assert row["error_message"] == "The model execution failed safely."
        for code, message in event_text:
            if code is not None:
                assert message == safe_error_message(code), (code, message)
        # And an unclassified failure is never retried, so it cannot leak a second time.
        assert attempt_statuses(engine, run_id) == [AttemptStatus.FAILED.value]
        assert_timeline_agrees(engine, run_id)
    finally:
        engine.dispose()
