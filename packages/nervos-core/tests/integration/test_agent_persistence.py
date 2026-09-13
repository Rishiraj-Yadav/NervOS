"""B1 Agent Instance and Run persistence integration tests."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from alembic import command
from alembic.config import Config
from nervos_core.application.agent_definitions import create_builtin_definition_registry
from nervos_core.application.agents import (
    AgentInstanceNotFound,
    AgentInstanceUnavailable,
    AgentService,
    InstanceConfiguration,
    RunNotFound,
    RunTransitionRejected,
)
from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.application.model_completion import ModelRequest, ModelResponse, StopOutcome
from nervos_core.application.model_providers import ModelProviderCatalog
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.runs import STAGE_B_LIMITS, InvalidRun, ModelUsage, Run, RunStatus
from nervos_core.infrastructure.database import create_session_factory, create_sqlite_engine
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.models import AgentInstanceRecord, RunRecord, UserRecord
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[4]
NOW = datetime(2026, 1, 1, tzinfo=UTC)
DEFAULT_DATABASE = (Path.home() / ".nervos" / "nervos.db").resolve(strict=False)


def database_metadata(path: Path) -> tuple[bool, int | None, int | None]:
    """Fingerprint database metadata without reading potentially sensitive contents."""
    if not path.exists():
        return False, None, None
    stat = path.stat()
    return True, stat.st_size, stat.st_mtime_ns


class _FakeCompletion:
    """Test-only provider used only to resolve preflight; it is never invoked here."""

    async def complete(self, request: ModelRequest) -> ModelResponse:  # pragma: no cover
        del request
        return ModelResponse("fixed", "test-provider", "Org/Model:v1", StopOutcome.STOP)


@pytest.fixture(autouse=True)
def protect_default_database() -> Iterator[None]:
    before = database_metadata(DEFAULT_DATABASE)
    yield
    assert database_metadata(DEFAULT_DATABASE) == before


@pytest.fixture
def persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[SqlAlchemyAgentPersistence, AgentService, sessionmaker[Session]]]:
    path = (tmp_path / "agents.db").resolve()
    assert path != DEFAULT_DATABASE
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(path))
    command.upgrade(Config(str(ROOT / "apps/api/alembic.ini")), "head")
    engine = create_sqlite_engine(path)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.add_all(
            [
                UserRecord(
                    username="user_a",
                    password_hash="x",
                    role="admin",
                    is_active=True,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                UserRecord(
                    username="user_b",
                    password_hash="x",
                    role="admin",
                    is_active=True,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
    adapter = SqlAlchemyAgentPersistence(factory)
    handlers = create_builtin_handler_registry()
    providers = ModelProviderCatalog(
        [
            ("test-provider", _FakeCompletion),
            ("new-provider", _FakeCompletion),
            ("old-provider", _FakeCompletion),
            ("other-provider", _FakeCompletion),
        ],
        known=["test-provider", "new-provider", "old-provider", "other-provider"],
    )
    yield (
        adapter,
        AgentService(
            adapter, create_builtin_definition_registry(), lambda: NOW, handlers, providers
        ),
        factory,
    )
    engine.dispose()


def test_instances_are_explicit_nonunique_and_owner_scoped(
    persistence: tuple[SqlAlchemyAgentPersistence, AgentService, sessionmaker[Session]],
) -> None:
    adapter, service, _ = persistence
    identity = AgentDefinitionId("nervos.chat", "1")
    first = service.create_instance(1, identity, " Chat ", "test-provider", "Org/Model:v1")
    second = service.create_instance(1, identity, "Chat", "test-provider", "Other.Model")
    assert first.display_name == second.display_name == "Chat"
    assert [item.id for item in adapter.list_instances(1, 20, None)] == [second.id, first.id]
    with pytest.raises(AgentInstanceNotFound):
        adapter.get_instance(2, first.id)
    with pytest.raises(AgentInstanceNotFound):
        adapter.get_instance(2, 9999)


def test_atomic_run_creation_copies_snapshot_and_disable_blocks_new_run(
    persistence: tuple[SqlAlchemyAgentPersistence, AgentService, sessionmaker[Session]],
) -> None:
    adapter, service, factory = persistence
    identity = AgentDefinitionId("nervos.chat", "1")
    instance = service.create_instance(1, identity, "Chat", "test-provider", "Org/Model:v1")
    prompt = "line one\n\tcode()\r\nline two"
    run = service.create_run(1, instance.id, prompt)
    assert run.input_text == prompt
    assert run.model_name == "Org/Model:v1"
    assert run.limits == STAGE_B_LIMITS
    service.update_instance(1, instance.id, "Chat", "other-provider", "New/Model")
    assert adapter.get_run(1, run.id).model_name == "Org/Model:v1"
    service.set_enabled(1, instance.id, False)
    with pytest.raises(AgentInstanceUnavailable):
        service.create_run(1, instance.id, "another")
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(RunRecord)) == 1
    with pytest.raises(RunNotFound):
        adapter.get_run(2, run.id)


def test_expected_state_transitions_are_terminal_and_owner_scoped(
    persistence: tuple[SqlAlchemyAgentPersistence, AgentService, sessionmaker[Session]],
) -> None:
    adapter, service, _ = persistence
    instance = service.create_instance(
        1, AgentDefinitionId("nervos.chat", "1"), "Chat", "test-provider", "model"
    )
    run = service.create_run(1, instance.id, "hello")
    running = adapter.mark_running(1, run.id, NOW)
    assert running.status is RunStatus.RUNNING
    succeeded = service.succeed(
        1, run.id, "result\n```code```", "stop", ModelUsage(input_tokens=2), 5
    )
    assert succeeded.status is RunStatus.SUCCEEDED
    with pytest.raises(RunTransitionRejected):
        adapter.mark_failed(1, run.id, "model_error", "safe", ModelUsage(), 6, NOW)
    with pytest.raises(RunTransitionRejected):
        adapter.mark_succeeded(2, run.id, "other", None, ModelUsage(), 7, NOW)


def test_blank_terminal_text_is_rejected_identically_by_domain_and_database(
    persistence: tuple[SqlAlchemyAgentPersistence, AgentService, sessionmaker[Session]],
) -> None:
    adapter, service, factory = persistence
    instance = service.create_instance(
        1, AgentDefinitionId("nervos.chat", "1"), "Chat", "test-provider", "model"
    )
    blank_texts = (
        " ",
        chr(9),
        chr(10),
        chr(13) + chr(10),
        " " + chr(9) + chr(13) + chr(10) + " ",
        chr(0x00A0) + chr(0x2003),
        chr(0x2028),
        chr(0x3000),
    )
    for blank in blank_texts:
        run = service.create_run(1, instance.id, "hello")
        adapter.mark_running(1, run.id, NOW)
        with pytest.raises(InvalidRun):
            service.succeed(1, run.id, blank, None, ModelUsage(), 1)
        with pytest.raises(InvalidRun):
            service.fail(1, run.id, "model_error", blank, ModelUsage(), 1)
        with pytest.raises(IntegrityError), factory.begin() as session:
            session.execute(
                text(
                    "UPDATE runs SET status='succeeded', finished_at=:finished, "
                    "output_text=:output, elapsed_ms=1 WHERE id=:run_id"
                ),
                {"finished": "2026-01-01 00:00:01", "output": blank, "run_id": run.id},
            )
        with pytest.raises(IntegrityError), factory.begin() as session:
            session.execute(
                text(
                    "UPDATE runs SET status='failed', finished_at=:finished, "
                    "error_code='model_error', error_message=:message, "
                    "elapsed_ms=1 WHERE id=:run_id"
                ),
                {"finished": "2026-01-01 00:00:01", "message": blank, "run_id": run.id},
            )
    multiline = "\tanswer\nsecond line\n"
    run = service.create_run(1, instance.id, "hello")
    adapter.mark_running(1, run.id, NOW)
    succeeded = service.succeed(1, run.id, multiline, "stop", ModelUsage(input_tokens=1), 5)
    assert succeeded.output_text == multiline
    assert adapter.get_run(1, run.id).output_text == multiline
    with pytest.raises(InvalidRun):
        service.create_run(1, instance.id, "\t")


def test_every_database_valid_run_shape_reconstructs_as_a_domain_run(
    persistence: tuple[SqlAlchemyAgentPersistence, AgentService, sessionmaker[Session]],
) -> None:
    adapter, service, factory = persistence
    instance = service.create_instance(
        1, AgentDefinitionId("nervos.chat", "1"), "Chat", "test-provider", "model"
    )
    rows = (
        {"status": "created"},
        {"status": "running", "started_at": "2026-01-01 00:00:01"},
        {
            "status": "succeeded",
            "started_at": "2026-01-01 00:00:01",
            "finished_at": "2026-01-01 00:00:02",
            "output_text": "\tcode:\n\tprint('x')\n",
            "elapsed_ms": 1,
        },
        {
            "status": "failed",
            "started_at": "2026-01-01 00:00:01",
            "finished_at": "2026-01-01 00:00:02",
            "error_code": "model_error",
            "error_message": "safe failure",
            "elapsed_ms": 1,
        },
    )
    run_ids: list[int] = []
    for row in rows:
        values = {
            "agent_instance_id": instance.id,
            "agent_key": "nervos.chat",
            "agent_definition_version": "1",
            "model_provider": "test-provider",
            "model_name": "model",
            "input_text": "hello",
            "input_max_bytes": 8000,
            "input_max_code_points": 4000,
            "output_max_bytes": 32000,
            "output_max_code_points": 16000,
            "provider_timeout_ms": 60000,
            "max_output_tokens": 1024,
            "max_model_calls": 1,
            "created_at": "2026-01-01 00:00:00",
            **row,
        }
        columns = ", ".join(values)
        parameters = ", ".join(f":{column}" for column in values)
        with factory.begin() as session:
            result = session.execute(
                text(f"INSERT INTO runs ({columns}) VALUES ({parameters}) RETURNING id"), values
            )
            run_ids.append(int(result.scalar_one()))
    for row, run_id in zip(rows, run_ids, strict=True):
        reconstructed = adapter.get_run(1, run_id)
        assert reconstructed.status == RunStatus(row["status"])
        assert reconstructed.output_text == row.get("output_text")
        assert reconstructed.error_message == row.get("error_message")
        assert reconstructed.limits == STAGE_B_LIMITS


def test_disable_racing_run_creation_is_linearizable(
    persistence: tuple[SqlAlchemyAgentPersistence, AgentService, sessionmaker[Session]],
) -> None:
    _, service, factory = persistence
    instance = service.create_instance(
        1, AgentDefinitionId("nervos.chat", "1"), "Chat", "test-provider", "model"
    )
    barrier = Barrier(2)

    def create() -> str:
        barrier.wait()
        try:
            service.create_run(1, instance.id, "racing input")
            return "created"
        except AgentInstanceUnavailable:
            return "unavailable"

    def disable() -> str:
        barrier.wait()
        service.set_enabled(1, instance.id, False)
        return "disabled"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(create), executor.submit(disable))
        results = {future.result() for future in futures}
    assert "disabled" in results
    with factory() as session:
        runs = tuple(session.scalars(select(RunRecord)))
        stored = session.get(AgentInstanceRecord, instance.id)
        assert stored is not None and stored.enabled is False
    assert len(runs) in {0, 1}
    if runs:
        assert runs[0].model_provider == "test-provider"
        assert runs[0].model_name == "model"


def test_provider_model_update_racing_run_creation_has_coherent_snapshot(
    persistence: tuple[SqlAlchemyAgentPersistence, AgentService, sessionmaker[Session]],
) -> None:
    _, service, _ = persistence
    instance = service.create_instance(
        1, AgentDefinitionId("nervos.chat", "1"), "Chat", "old-provider", "old-model"
    )
    barrier = Barrier(2)

    def create() -> Run:
        barrier.wait()
        return service.create_run(1, instance.id, "racing input")

    def update_configuration() -> None:
        barrier.wait()
        service.update_instance(1, instance.id, "Chat", "new-provider", "new-model")

    with ThreadPoolExecutor(max_workers=2) as executor:
        create_future = executor.submit(create)
        update_future = executor.submit(update_configuration)
        run = create_future.result()
        update_future.result()
    assert (run.model_provider, run.model_name) in {
        ("old-provider", "old-model"),
        ("new-provider", "new-model"),
    }


def test_competing_terminal_writers_have_one_consistent_winner(
    persistence: tuple[SqlAlchemyAgentPersistence, AgentService, sessionmaker[Session]],
) -> None:
    adapter, service, _ = persistence
    instance = service.create_instance(
        1, AgentDefinitionId("nervos.chat", "1"), "Chat", "test-provider", "model"
    )
    run = service.create_run(1, instance.id, "hello")
    adapter.mark_running(1, run.id, NOW)
    barrier = Barrier(2)

    def succeed() -> str:
        barrier.wait()
        try:
            adapter.mark_succeeded(1, run.id, "ok", "stop", ModelUsage(), 1, NOW)
            return "succeeded"
        except RunTransitionRejected:
            return "rejected"

    def fail() -> str:
        barrier.wait()
        try:
            adapter.mark_failed(1, run.id, "model_error", "safe", ModelUsage(), 1, NOW)
            return "failed"
        except RunTransitionRejected:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(succeed), executor.submit(fail))
        results = {future.result() for future in futures}
    assert "rejected" in results
    final = adapter.get_run(1, run.id)
    assert final.status in {RunStatus.SUCCEEDED, RunStatus.FAILED}
    assert (final.output_text is None) != (final.error_code is None)


@pytest.mark.parametrize("operation", ["read", "write", "run_create", "transition"])
def test_sqlalchemy_failures_are_translated_with_preserved_cause(
    persistence: tuple[SqlAlchemyAgentPersistence, AgentService, sessionmaker[Session]],
    operation: str,
) -> None:
    adapter, service, factory = persistence
    instance = service.create_instance(
        1, AgentDefinitionId("nervos.chat", "1"), "Chat", "test-provider", "model"
    )
    run = service.create_run(1, instance.id, "hello")
    if operation == "transition":
        adapter.mark_running(1, run.id, NOW)
    with factory.begin() as session:
        if operation in {"read", "write"}:
            session.execute(text("DROP TABLE runs"))
            session.execute(text("DROP TABLE agent_instances"))
        else:
            session.execute(text("DROP TABLE runs"))
    actions = {
        "read": lambda: adapter.get_instance(1, instance.id),
        "write": lambda: adapter.update_instance(
            1, instance.id, InstanceConfiguration("Chat", "test-provider", "model"), NOW
        ),
        "run_create": lambda: service.create_run(1, instance.id, "again"),
        "transition": lambda: adapter.mark_succeeded(1, run.id, "ok", None, ModelUsage(), 1, NOW),
    }
    with pytest.raises(PersistenceUnavailable) as captured:
        actions[operation]()
    assert isinstance(captured.value.__cause__, OperationalError)
    assert str(captured.value) == ""


def test_foreign_keys_restrict_history_deletion(
    persistence: tuple[SqlAlchemyAgentPersistence, AgentService, sessionmaker[Session]],
) -> None:
    _, service, factory = persistence
    instance = service.create_instance(
        1, AgentDefinitionId("nervos.chat", "1"), "Chat", "test-provider", "model"
    )
    service.create_run(1, instance.id, "hello")
    with pytest.raises(IntegrityError), factory.begin() as session:
        session.execute(delete(AgentInstanceRecord).where(AgentInstanceRecord.id == instance.id))
    with pytest.raises(IntegrityError), factory.begin() as session:
        session.execute(delete(UserRecord).where(UserRecord.id == 1))
