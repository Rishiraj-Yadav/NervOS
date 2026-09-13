"""Coordinator integration tests: lifecycle, transaction boundaries, and failure classes."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from nervos_core.application.agent_definitions import (
    BuiltInAgentDefinitionRegistry,
    create_builtin_definition_registry,
)
from nervos_core.application.agents import (
    AgentInstanceNotFound,
    AgentInstanceUnavailable,
    AgentService,
    RunTransitionRejected,
)
from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.application.model_completion import (
    MODEL_OUTPUT_INCOMPLETE,
    MODEL_REFUSED,
    MODEL_RESPONSE_INVALID,
    MODEL_TIMED_OUT,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    StopOutcome,
)
from nervos_core.application.model_providers import ModelProviderCatalog
from nervos_core.application.run_coordinator import RunCoordinator
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.domain.agents import AgentDefinition, AgentDefinitionId
from nervos_core.domain.runs import InvalidRun, ModelUsage, RunLimits, RunStatus
from nervos_core.infrastructure.database import create_session_factory, create_sqlite_engine
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    RunRecord,
    UserRecord,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[4]
NOW = datetime(2026, 1, 1, tzinfo=UTC)
DEFAULT_DATABASE = (Path.home() / ".nervos" / "nervos.db").resolve(strict=False)
IDENTITY = AgentDefinitionId("nervos.chat", "1")


def database_metadata(path: Path) -> tuple[bool, int | None, int | None]:
    """Fingerprint database metadata without reading potentially sensitive contents."""
    if not path.exists():
        return False, None, None
    stat = path.stat()
    return True, stat.st_size, stat.st_mtime_ns


@pytest.fixture(autouse=True)
def protect_default_database() -> Iterator[None]:
    before = database_metadata(DEFAULT_DATABASE)
    yield
    assert database_metadata(DEFAULT_DATABASE) == before


class RecordingCompletion:
    """Deterministic provider double that records every request and invocation count."""

    def __init__(self, response: ModelResponse | None = None, error: Exception | None = None):
        self.requests: list[ModelRequest] = []
        self.started = asyncio.Event()
        self._release = asyncio.Event()
        self.response = response
        self.error = error
        self.block = False

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        self.started.set()
        if self.block:
            await self._release.wait()
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response

    def release(self) -> None:
        self._release.set()


def ok_response(text: str = "answer", usage: ModelUsage | None = None) -> ModelResponse:
    return ModelResponse(text, "test-provider", "opaque/model", StopOutcome.STOP, usage)


class Harness:
    """Coordinator plus the persistence handles needed to inspect committed state."""

    def __init__(
        self,
        adapter: SqlAlchemyAgentPersistence,
        service: AgentService,
        coordinator: RunCoordinator,
        completion: RecordingCompletion,
        factory: sessionmaker[Session],
        instance_id: int,
    ) -> None:
        self.adapter = adapter
        self.service = service
        self.coordinator = coordinator
        self.completion = completion
        self.factory = factory
        self.instance_id = instance_id


@pytest.fixture
def coordinator_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    yield from build_harness(tmp_path, monkeypatch)


def build_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    limits: RunLimits | None = None,
) -> Iterator[Harness]:
    path = (tmp_path / "coordinator.db").resolve()
    assert path != DEFAULT_DATABASE
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(path))
    command.upgrade(Config(str(ROOT / "apps/api/alembic.ini")), "head")
    engine = create_sqlite_engine(path)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add(
            UserRecord(
                username="user_a",
                password_hash="x",
                role="admin",
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    adapter = SqlAlchemyAgentPersistence(factory)
    handlers = create_builtin_handler_registry()
    completion = RecordingCompletion(ok_response())
    providers = ModelProviderCatalog(
        [("test-provider", lambda: completion)], known=["test-provider"]
    )
    definitions = (
        create_builtin_definition_registry()
        if limits is None
        else BuiltInAgentDefinitionRegistry([AgentDefinition(IDENTITY, "NervOS Chat", limits)])
    )
    service = AgentService(adapter, definitions, lambda: NOW, handlers, providers)
    instance = service.create_instance(1, IDENTITY, "Chat", "test-provider", "opaque/model")
    coordinator = RunCoordinator(service, handlers, providers, lambda: 0)
    yield Harness(adapter, service, coordinator, completion, factory, instance.id)
    engine.dispose()


def _instance_enabled(harness: Harness) -> bool:
    with harness.factory() as session:
        record = session.get(AgentInstanceRecord, harness.instance_id)
        assert record is not None
        return bool(record.enabled)


@pytest.mark.anyio
async def test_success_lifecycle_runs_exactly_one_call_and_returns_committed_run(
    coordinator_harness: Harness,
) -> None:
    harness = coordinator_harness
    harness.completion.response = ok_response("hello there", ModelUsage(11, 7, None))
    run = await harness.coordinator.execute(1, harness.instance_id, "prompt")
    assert run.status is RunStatus.SUCCEEDED
    assert run.output_text == "hello there"
    assert run.finish_reason == "stop"
    assert run.usage == ModelUsage(11, 7, None)
    assert run.usage.total_tokens is None
    assert run.elapsed_ms is not None
    assert harness.completion.calls == 1
    reloaded = harness.adapter.get_run(1, run.id)
    assert reloaded.status is RunStatus.SUCCEEDED
    assert reloaded.output_text == "hello there"


@pytest.mark.anyio
async def test_multiline_output_is_preserved_exactly(coordinator_harness: Harness) -> None:
    harness = coordinator_harness
    harness.completion.response = ok_response("\tline\nsecond line\n")
    run = await harness.coordinator.execute(1, harness.instance_id, "prompt")
    assert run.output_text == "\tline\nsecond line\n"


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (
            ModelResponse("x", "test-provider", "opaque/model", StopOutcome.INCOMPLETE),
            MODEL_OUTPUT_INCOMPLETE,
        ),
        (ModelResponse("x", "test-provider", "opaque/model", StopOutcome.REFUSED), MODEL_REFUSED),
        (
            ModelResponse("x", "test-provider", "opaque/model", StopOutcome.INVALID),
            MODEL_RESPONSE_INVALID,
        ),
        (
            ModelResponse("  \t\n", "test-provider", "opaque/model", StopOutcome.STOP),
            MODEL_RESPONSE_INVALID,
        ),
        (
            ModelResponse("x\x00", "test-provider", "opaque/model", StopOutcome.STOP),
            MODEL_RESPONSE_INVALID,
        ),
        (
            ModelResponse("x" * 40000, "test-provider", "opaque/model", StopOutcome.STOP),
            "model_output_too_large",
        ),
        (
            ModelResponse("x", "other-provider", "opaque/model", StopOutcome.STOP),
            MODEL_RESPONSE_INVALID,
        ),
    ],
)
@pytest.mark.anyio
async def test_execution_failures_persist_safe_failed_runs(
    coordinator_harness: Harness, response: ModelResponse, expected_code: str
) -> None:
    harness = coordinator_harness
    harness.completion.response = response
    run = await harness.coordinator.execute(1, harness.instance_id, "prompt")
    assert run.status is RunStatus.FAILED
    assert run.error_code == expected_code
    assert run.output_text is None
    assert run.finish_reason is None
    assert run.error_message is not None
    assert harness.completion.calls == 1


@pytest.mark.anyio
async def test_provider_error_is_persisted_as_a_failed_run(
    coordinator_harness: Harness,
) -> None:
    harness = coordinator_harness
    harness.completion.error = ModelProviderError(MODEL_RESPONSE_INVALID)
    run = await harness.coordinator.execute(1, harness.instance_id, "prompt")
    assert run.status is RunStatus.FAILED
    assert run.error_code == MODEL_RESPONSE_INVALID
    assert harness.completion.calls == 1


@pytest.mark.anyio
async def test_blank_input_is_rejected_before_any_run_or_call(
    coordinator_harness: Harness,
) -> None:
    harness = coordinator_harness
    with pytest.raises(InvalidRun):
        await harness.coordinator.execute(1, harness.instance_id, "   ")
    assert harness.completion.calls == 0
    _assert_run_count(harness, 0)


@pytest.mark.anyio
async def test_non_running_run_is_rejected_without_a_call(coordinator_harness: Harness) -> None:
    harness = coordinator_harness
    _assert_run_count(harness, 0)
    with pytest.raises(AgentInstanceNotFound):
        await harness.coordinator.execute(1, 9999, "prompt")
    with pytest.raises(AgentInstanceNotFound):
        await harness.coordinator.execute(2, harness.instance_id, "prompt")
    assert harness.completion.calls == 0
    _assert_run_count(harness, 0)


@pytest.mark.anyio
async def test_disabled_instance_is_rejected_without_a_run_or_call(
    coordinator_harness: Harness,
) -> None:
    harness = coordinator_harness
    harness.service.set_enabled(1, harness.instance_id, False)
    assert _instance_enabled(harness) is False
    with pytest.raises(AgentInstanceUnavailable):
        await harness.coordinator.execute(1, harness.instance_id, "prompt")
    assert harness.completion.calls == 0
    _assert_run_count(harness, 0)


def _assert_run_count(harness: Harness, expected: int) -> None:
    with harness.factory() as session:
        assert session.scalar(select(func.count()).select_from(RunRecord)) == expected


@pytest.mark.anyio
async def test_no_transaction_is_open_across_the_provider_await(
    coordinator_harness: Harness,
) -> None:
    harness = coordinator_harness
    harness.completion.block = True
    harness.completion.response = ok_response()
    task = asyncio.create_task(harness.coordinator.execute(1, harness.instance_id, "prompt"))
    await asyncio.wait_for(harness.completion.started.wait(), timeout=5)
    assert harness.completion.calls == 1

    # The coordinator holds no session or transaction, so an independent write succeeds and
    # observes the committed `running` row while the provider await is still pending.
    with harness.factory.begin() as session:
        assert session.scalar(select(RunRecord.status)) == RunStatus.RUNNING.value
        assert session.scalar(select(func.count()).select_from(RunRecord)) == 1

    harness.completion.release()
    run = await asyncio.wait_for(task, timeout=5)
    assert run.status is RunStatus.SUCCEEDED


@pytest.mark.anyio
async def test_timeout_becomes_a_failed_run_without_a_second_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    iterator = build_harness(tmp_path, monkeypatch, limits=RunLimits(provider_timeout_ms=50))
    harness = next(iterator)
    try:
        harness.completion.block = True
        run = await asyncio.wait_for(
            harness.coordinator.execute(1, harness.instance_id, "prompt"), timeout=10
        )
        assert run.status is RunStatus.FAILED
        assert run.error_code == MODEL_TIMED_OUT
        assert run.output_text is None
        assert harness.completion.calls == 1
    finally:
        with pytest.raises(StopIteration):
            next(iterator)


@pytest.mark.anyio
async def test_external_cancellation_propagates_and_does_not_fake_success(
    coordinator_harness: Harness,
) -> None:
    harness = coordinator_harness
    harness.completion.block = True
    task = asyncio.create_task(harness.coordinator.execute(1, harness.instance_id, "prompt"))
    await asyncio.wait_for(harness.completion.started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert harness.completion.calls == 1
    with harness.factory() as session:
        assert session.scalar(select(RunRecord.status)) == RunStatus.RUNNING.value


@pytest.mark.anyio
async def test_provider_failure_plus_failed_failure_persistence_raises_platform_error(
    coordinator_harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = coordinator_harness
    harness.completion.error = ModelProviderError(MODEL_OUTPUT_INCOMPLETE)

    def _broken_fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise PersistenceUnavailable

    monkeypatch.setattr(harness.adapter, "mark_failed", _broken_fail)
    with pytest.raises(PersistenceUnavailable):
        await harness.coordinator.execute(1, harness.instance_id, "prompt")
    run = harness.adapter.get_run(1, 1)
    assert run.status is RunStatus.RUNNING
    assert run.error_code is None


@pytest.mark.anyio
async def test_success_persistence_failure_never_exposes_the_answer(
    coordinator_harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = coordinator_harness
    secret_answer = "SYNTHETIC-ANSWER-MUST-NOT-ESCAPE"
    harness.completion.response = ok_response(secret_answer)

    def _broken_succeed(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise PersistenceUnavailable

    monkeypatch.setattr(harness.adapter, "mark_succeeded", _broken_succeed)
    with pytest.raises(PersistenceUnavailable) as error:
        await harness.coordinator.execute(1, harness.instance_id, "prompt")
    assert secret_answer not in str(error.value)
    run = harness.adapter.get_run(1, 1)
    assert run.status is RunStatus.RUNNING
    assert run.output_text is None
    assert harness.completion.calls == 1


@pytest.mark.anyio
async def test_terminal_transition_conflict_is_not_retried(
    coordinator_harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = coordinator_harness
    monkeypatch.setattr(harness.adapter, "mark_succeeded", _rejected)
    with pytest.raises(RunTransitionRejected):
        await harness.coordinator.execute(1, harness.instance_id, "prompt")
    assert harness.completion.calls == 1


@pytest.mark.anyio
async def test_start_persistence_failure_makes_no_provider_call(
    coordinator_harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = coordinator_harness

    def _broken_start(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise PersistenceUnavailable

    monkeypatch.setattr(harness.adapter, "mark_running", _broken_start)
    with pytest.raises(PersistenceUnavailable):
        await harness.coordinator.execute(1, harness.instance_id, "prompt")
    assert harness.completion.calls == 0
    run = harness.adapter.get_run(1, 1)
    assert run.status is RunStatus.CREATED


@pytest.mark.anyio
async def test_snapshot_survives_instance_update_and_disable(
    coordinator_harness: Harness,
) -> None:
    harness = coordinator_harness
    run = await harness.coordinator.execute(1, harness.instance_id, "prompt")
    assert run.model_name == "opaque/model"
    harness.service.update_instance(1, harness.instance_id, "Chat", "test-provider", "other/model")
    harness.service.set_enabled(1, harness.instance_id, False)
    reloaded = harness.adapter.get_run(1, run.id)
    assert reloaded.model_name == "opaque/model"
    assert reloaded.status is RunStatus.SUCCEEDED


def _rejected(*args: object, **kwargs: object) -> None:
    del args, kwargs
    raise RunTransitionRejected
