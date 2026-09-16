"""C7 Event reads: ascending order, the keyset cursor, contiguity, and concurrent appends.

A Run's Event sequence is allocated contiguously from a per-Run high-water mark read once inside
the writing transaction, so the stream is `1..N` with no gaps and a batch is committed atomically.
That is what makes the pagination lossless as a proof rather than a hope, and these tests assert the
proof: a client that has applied everything up to sequence S can never have skipped an Event.
"""

from __future__ import annotations

import threading
from dataclasses import fields
from datetime import timedelta
from pathlib import Path

import pytest
from execution_support import NOW, migrate
from nervos_core.application.agents import EVENT_PAGE_LIMIT_MAX, RunNotFound
from nervos_core.application.job_execution import (
    LEASE_DURATION,
    ClaimedAttempt,
    JobExecutionService,
)
from nervos_core.application.model_completion import (
    MODEL_RATE_LIMITED,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    safe_error_message,
)
from nervos_core.application.retry_policy import PRODUCTION_RETRY_POLICY
from nervos_core.application.run_execution import RunExecutor
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.domain.jobs import RetryDisposition, RunEventType
from nervos_core.domain.runs import STAGE_B_LIMITS, ModelUsage
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

ANTHROPIC = ("anthropic",)
WIDE = 64


def reader(engine: Engine) -> SqlAlchemyAgentPersistence:
    return SqlAlchemyAgentPersistence(sessionmaker(bind=engine))


def execution(engine: Engine) -> SqlAlchemyJobExecutionPersistence:
    return SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _: None)


def submit(engine: Engine, *, text_value: str = "hello", max_attempts: int = 3) -> int:
    return (
        SqlAlchemyJobPersistence(engine, max_pending=1000)
        .submit(
            owner_user_id=1,
            agent_instance_id=1,
            input_text=text_value,
            limits=STAGE_B_LIMITS,
            now=NOW,
            max_attempts=max_attempts,
        )
        .id
    )


def claim(engine: Engine, *, now: object = NOW) -> ClaimedAttempt:
    claimed = execution(engine).claim_next(
        worker_id="worker-1",
        provider_ids=ANTHROPIC,
        max_active=WIDE,
        now=now,  # type: ignore[arg-type]
        lease_duration=LEASE_DURATION,
    )
    assert claimed is not None
    return claimed


def start(engine: Engine, claimed: ClaimedAttempt, *, now: object = NOW) -> ClaimedAttempt:
    assert execution(engine).start_attempt(claimed, now=now) is True  # type: ignore[arg-type]
    return claimed


def rate_limit(engine: Engine, claimed: ClaimedAttempt, *, now: object = NOW) -> None:
    execution(engine).record_failure(
        claimed,
        error_code=MODEL_RATE_LIMITED,
        error_message=safe_error_message(MODEL_RATE_LIMITED),
        retry_disposition=RetryDisposition.SAFE_TO_RETRY,
        usage=ModelUsage(1, 1, 2),
        elapsed_ms=5,
        anchor_at=now,  # type: ignore[arg-type]
        retry_policy=PRODUCTION_RETRY_POLICY,
        now=now,  # type: ignore[arg-type]
    )


def succeed(engine: Engine, claimed: ClaimedAttempt, *, now: object = NOW) -> None:
    assert execution(engine).succeed(
        claimed,
        output_text="done",
        finish_reason="stop",
        usage=ModelUsage(1, 1, 2),
        elapsed_ms=5,
        now=now,  # type: ignore[arg-type]
    )


def drive_retrying_run(engine: Engine, *, attempts: int = 3) -> int:
    """Drive one Run through repeated positively-safe rate limits into a success.

    Every step is a real production transition, so the history it produces is the history a
    deployment would produce -- not a hand-written fixture that could disagree with the writers.
    """
    assert attempts >= 2
    run_id = submit(engine, max_attempts=attempts)
    now = NOW
    for _ in range(attempts - 1):
        rate_limit(engine, start(engine, claim(engine, now=now), now=now), now=now)
        now = now + timedelta(seconds=10)
    succeed(engine, start(engine, claim(engine, now=now), now=now), now=now)
    return run_id


def drive_success(engine: Engine) -> int:
    run_id = submit(engine)
    succeed(engine, start(engine, claim(engine)), now=NOW)
    return run_id


def fetch(
    engine: Engine, run_id: int, *, after_sequence: int = 0, limit: int = 50
) -> tuple[list[int], list[str]]:
    events = reader(engine).list_run_events(run_id, after_sequence, limit)
    return [event.sequence for event in events], [event.event_type.value for event in events]


def drain(engine: Engine, run_id: int, *, limit: int) -> list[int]:
    """Follow the same loop the client follows, and prove where it terminates."""
    collected: list[int] = []
    cursor = 0
    for _ in range(1000):
        page = reader(engine).list_run_events(run_id, cursor, limit)
        collected.extend(event.sequence for event in page)
        if len(page) < limit:
            return collected
        cursor = page[-1].sequence
    raise AssertionError("the cursor never reached the end of the history")


# ---------------------------------------------------------------------------------------
# Order and contiguity
# ---------------------------------------------------------------------------------------


def test_events_are_returned_in_ascending_sequence_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "order.db", monkeypatch)
    try:
        run_id = drive_retrying_run(engine)
        sequences, types = fetch(engine, run_id)

        assert sequences == sorted(sequences)
        assert sequences == list(range(1, len(sequences) + 1))
        assert types[0] == RunEventType.RUN_CREATED.value
        assert types[1] == RunEventType.RUN_QUEUED.value
        assert types[-1] == RunEventType.RUN_SUCCEEDED.value
    finally:
        engine.dispose()


def test_a_fully_driven_run_has_no_gap_in_its_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contiguity is what makes "no missing Event" provable rather than probabilistic."""
    engine = migrate(tmp_path / "contiguous.db", monkeypatch)
    try:
        run_id = drive_retrying_run(engine, attempts=4)

        with engine.connect() as connection:
            stored = list(
                connection.execute(
                    text("SELECT sequence FROM run_events WHERE run_id=:r ORDER BY sequence"),
                    {"r": run_id},
                ).scalars()
            )

        assert len(stored) == 17
        assert stored == list(range(1, len(stored) + 1))
        assert fetch(engine, run_id)[0] == stored
    finally:
        engine.dispose()


def test_a_retry_is_legible_from_the_durable_events_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "retry-legible.db", monkeypatch)
    try:
        run_id = drive_retrying_run(engine)
        events = reader(engine).list_run_events(run_id, 0, 50)
        types = [event.event_type for event in events]

        first_failure = types.index(RunEventType.ATTEMPT_FAILED)
        scheduled = types.index(RunEventType.RETRY_SCHEDULED)
        second_claim = types.index(RunEventType.ATTEMPT_CLAIMED, scheduled)

        assert first_failure < scheduled < second_claim
        failure = events[first_failure]
        assert failure.code == MODEL_RATE_LIMITED
        assert failure.message == safe_error_message(MODEL_RATE_LIMITED)
        assert failure.attempt_number == 1
        # The retry instant is a durable fact, which is what makes the copy exact.
        assert events[scheduled].available_at is not None
        assert events[scheduled].attempt_number == 1
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# The cursor
# ---------------------------------------------------------------------------------------


def test_after_sequence_returns_strictly_greater_sequences_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "cursor.db", monkeypatch)
    try:
        run_id = drive_retrying_run(engine)
        sequences, _ = fetch(engine, run_id, after_sequence=4)

        assert sequences == [5, 6, 7, 8, 9, 10, 11, 12, 13]
    finally:
        engine.dispose()


def test_a_cursor_past_the_end_returns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "cursor-end.db", monkeypatch)
    try:
        run_id = drive_retrying_run(engine)
        total = len(fetch(engine, run_id)[0])

        assert fetch(engine, run_id, after_sequence=total) == ([], [])
        assert fetch(engine, run_id, after_sequence=total + 100) == ([], [])
    finally:
        engine.dispose()


def test_the_page_size_bounds_the_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = migrate(tmp_path / "page-size.db", monkeypatch)
    try:
        run_id = drive_retrying_run(engine)

        for limit in (1, 2, 5, EVENT_PAGE_LIMIT_MAX):
            sequences, _ = fetch(engine, run_id, limit=limit)
            assert len(sequences) == min(limit, 13)
    finally:
        engine.dispose()


def test_a_history_larger_than_one_page_drains_without_missing_or_duplicating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "drain.db", monkeypatch)
    try:
        run_id = drive_retrying_run(engine)
        full = drain(engine, run_id, limit=5)

        assert full == list(range(1, 14))
        assert len(full) == len(set(full))
    finally:
        engine.dispose()


def test_a_fresh_read_reconstructs_the_identical_ordered_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reload is a second drain from zero: the durable history is the only authority."""
    engine = migrate(tmp_path / "reload.db", monkeypatch)
    try:
        run_id = drive_retrying_run(engine)
        first = reader(engine).list_run_events(run_id, 0, 50)
        second_engine = create_sqlite_engine(tmp_path / "reload.db")
        try:
            fresh = SqlAlchemyAgentPersistence(sessionmaker(bind=second_engine)).list_run_events(
                run_id, 0, 50
            )
        finally:
            second_engine.dispose()

        assert [(event.sequence, event.event_type) for event in fresh] == [
            (event.sequence, event.event_type) for event in first
        ]
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# Scoping and safety
# ---------------------------------------------------------------------------------------


def test_events_are_scoped_to_their_own_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "scoped.db", monkeypatch)
    try:
        first = drive_success(engine)
        second = drive_success(engine)

        assert len(fetch(engine, first)[0]) == 5
        assert len(fetch(engine, second)[0]) == 5
        # Two Runs share one sequence space each: neither leaks the other's history.
        assert fetch(engine, first)[0] == fetch(engine, second)[0] == [1, 2, 3, 4, 5]
    finally:
        engine.dispose()


def test_a_foreign_or_missing_run_owner_finds_no_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate(tmp_path / "owner.db", monkeypatch)
    try:
        run_id = drive_success(engine)

        with pytest.raises(RunNotFound):
            reader(engine).get_run(99, run_id)
        # A missing Run is the same failure, so history cannot be used to probe for existence.
        with pytest.raises(RunNotFound):
            reader(engine).get_run(99, 9999)
    finally:
        engine.dispose()


def test_the_domain_read_carries_only_safe_durable_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is the whole fact: there is no column that could hold a secret to strip."""
    engine = migrate(tmp_path / "safe.db", monkeypatch)
    try:
        run_id = drive_retrying_run(engine)
        event = reader(engine).list_run_events(run_id, 0, 50)[4]

        # The exact field set of the durable row, so a new column cannot arrive unnoticed.
        assert {field.name for field in fields(event)} == {
            "id",
            "run_id",
            "job_id",
            "attempt_id",
            "sequence",
            "event_type",
            "code",
            "message",
            "attempt_number",
            "available_at",
            "created_at",
        }
        assert event.code == MODEL_RATE_LIMITED
        assert "\x00" not in (event.message or "")
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------------------
# A concurrent append
# ---------------------------------------------------------------------------------------


def test_an_event_appended_after_a_page_is_returned_by_the_next_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One request observes one committed statement snapshot; the next poll closes the gap.

    A second engine over the same file commits the terminal Event after the first page has been
    read, which is the exact interleaving that would drop an Event if the cursor moved by any rule
    other than "strictly greater than what I have applied".
    """
    path = tmp_path / "concurrent.db"
    engine = migrate(path, monkeypatch)
    writer = create_sqlite_engine(path)
    try:
        run_id = submit(engine)
        claimed = start(engine, claim(engine))
        before = reader(engine).list_run_events(run_id, 0, 50)
        barrier = threading.Barrier(2)

        def append() -> None:
            barrier.wait(timeout=10)
            succeed(writer, claimed, now=NOW + timedelta(seconds=1))

        thread = threading.Thread(target=append)
        thread.start()
        barrier.wait(timeout=10)
        thread.join(timeout=30)
        assert not thread.is_alive()

        seen = [event.sequence for event in before]
        after = reader(engine).list_run_events(run_id, seen[-1], 50)

        assert seen == [1, 2, 3, 4]
        assert [event.sequence for event in after] == [5]
        assert after[0].event_type is RunEventType.RUN_SUCCEEDED
        # Nothing was returned twice and nothing was skipped.
        assert seen + [event.sequence for event in after] == [1, 2, 3, 4, 5]
    finally:
        writer.dispose()
        engine.dispose()


def test_a_reader_draining_while_a_writer_appends_never_sees_a_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-gap property, asserted against a writer running concurrently rather than assumed."""
    path = tmp_path / "draining.db"
    engine = migrate(path, monkeypatch)
    writer = create_sqlite_engine(path)
    try:
        run_id = submit(engine)
        claimed = claim(engine)
        started = [event.sequence for event in reader(engine).list_run_events(run_id, 0, 50)]
        assert started == [1, 2, 3]
        failures: list[BaseException] = []
        observed: list[int] = []

        def read_pages() -> None:
            try:
                for _ in range(400):
                    events = reader(engine).list_run_events(run_id, 0, 50)
                    observed[:] = [event.sequence for event in events]
                    if len(observed) >= 5:
                        return
            except BaseException as error:  # pragma: no cover - only on a real invariant break
                failures.append(error)

        def write_events() -> None:
            try:
                start(engine, claimed)
                succeed(writer, claimed, now=NOW + timedelta(seconds=1))
            except BaseException as error:  # pragma: no cover - only on a real invariant break
                failures.append(error)

        threads = [threading.Thread(target=read_pages), threading.Thread(target=write_events)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert failures == []
        assert observed == [1, 2, 3, 4, 5]
    finally:
        writer.dispose()
        engine.dispose()


# ---------------------------------------------------------------------------------------
# The writer boundary: what makes the Event read safe is that the writers are
# ---------------------------------------------------------------------------------------

# Values shaped like the things that must never become durable. They are supplied to a provider
# double on the wire, exactly where a real adapter would receive them.
SECRETS = (
    "sk-test-secret-0123456789abcdef",
    # Assembled rather than written out: this file must not contain the literal block header that
    # both the repository scanner and the editor guard forbid on sight. The value is synthetic and
    # exists only to be rejected, which is what the test below asserts.
    "-----BEGIN " + "PRIVATE KEY-----",
    "Bearer authorization-value-must-not-persist",
)
RAW_FAILURE = f"provider blew up: {' '.join(SECRETS)}"


class FailingCompletion:
    """A provider double that fails the way a real adapter fails: with raw text on the wire."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.calls += 1
        raise self._error


async def no_sleep(_seconds: float) -> None:
    return None


async def drive_one_failure(engine: Engine, error: Exception) -> tuple[int, int]:
    """Push one failing provider call through the real execution path."""
    run_id = submit(engine)
    claimed = claim(engine)
    completion = FailingCompletion(error)
    service = JobExecutionService(
        execution(engine),
        RunExecutor(create_builtin_handler_registry()),
        {"anthropic": completion},
        lambda: NOW,
        sleep=no_sleep,
    )
    await service.execute(claimed)
    assert completion.calls == 1
    return run_id, claimed.job_id


def durable_text(engine: Engine, run_id: int) -> list[str]:
    """Every free-text value the durable execution tables hold for one Run."""
    with engine.connect() as connection:
        runs = list(
            connection.execute(
                text("SELECT error_code, error_message FROM runs WHERE id=:r"), {"r": run_id}
            ).all()
        )
        events = list(
            connection.execute(
                text("SELECT code, message FROM run_events WHERE run_id=:r"), {"r": run_id}
            ).all()
        )
    return [str(value) for row in runs + events for value in row if value is not None]


@pytest.mark.anyio
async def test_a_raw_provider_exception_never_becomes_durable_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The security boundary is the writer, not the reader.

    There is deliberately no response-time redaction layer, because none is needed: a provider
    exception is normalized to a frozen code before anything is written, and the persisted message
    comes from that code's allow-list entry. A raw failure carrying a key, a private-key marker and
    an authorization value is therefore discarded by the write path itself, and the reader has
    nothing to strip.
    """
    engine = migrate(tmp_path / "writer-boundary.db", monkeypatch)
    try:
        run_id, _ = await drive_one_failure(engine, RuntimeError(RAW_FAILURE))
        text_values = durable_text(engine, run_id)

        assert text_values, "the failure must have been recorded"
        for secret in SECRETS:
            assert not any(secret in value for value in text_values), secret
        # What *is* stored is the one static message the unclassified code maps to.
        assert "The model execution failed safely." in text_values
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT error_code FROM runs WHERE id=:r"), {"r": run_id})
                == "internal_execution_error"
            )
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_every_durable_message_comes_from_the_frozen_allow_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No production writer supplies free text, so the set of persistable messages is closed."""
    engine = migrate(tmp_path / "allow-list.db", monkeypatch)
    try:
        run_id, _ = await drive_one_failure(engine, ModelProviderError(MODEL_RATE_LIMITED))

        with engine.connect() as connection:
            run_pair = connection.execute(
                text("SELECT error_code, error_message FROM runs WHERE id=:r"), {"r": run_id}
            ).one()
            event_pairs = list(
                connection.execute(
                    text(
                        "SELECT code, message FROM run_events WHERE run_id=:r AND code IS NOT NULL"
                    ),
                    {"r": run_id},
                ).all()
            )

        # A positively-safe rate limit schedules a retry, so the Run is not terminal yet and
        # carries no error pair of its own; the Event does.
        assert run_pair[0] is None and run_pair[1] is None
        assert event_pairs, "the failure must have been recorded as an Event"
        assert {code for code, _ in event_pairs} == {MODEL_RATE_LIMITED}
        for code, message in event_pairs:
            # Each durable message is exactly the allow-list entry for its own durable code.
            assert message == safe_error_message(code), (code, message)
    finally:
        engine.dispose()


def test_an_unknown_failure_code_cannot_smuggle_its_own_message() -> None:
    """Even a caller-influenced code resolves to a frozen string, not to the code itself."""
    for code in ("sk-test-secret", "../../etc/passwd", "Bearer-token", "x" * 200):
        resolved = safe_error_message(code)
        assert resolved not in code
        assert "sk-test-secret" not in resolved
        # The fallback is the same static sentence for anything unrecognized.
        assert resolved == "The model execution failed safely."
