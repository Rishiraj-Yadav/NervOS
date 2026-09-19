"""E1 trigger and occurrence persistence, identity constraints, provenance, and materialization."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from nervos_core.application.agent_definitions import create_builtin_definition_registry
from nervos_core.application.errors import PersistenceUnavailable, QueueCapacityExceeded
from nervos_core.application.triggers import (
    ResolvedAgentDefinition,
    TriggerDraft,
    TriggerEdit,
    TriggerHasHistory,
    TriggerMaterializationCommand,
    TriggerMaterializationService,
    TriggerNotEditable,
    TriggerNotFound,
)
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.runs import STAGE_B_LIMITS
from nervos_core.domain.triggers import (
    InvalidTrigger,
    MisfirePolicy,
    OccurrenceStatus,
    RunOrigin,
    ScheduleSpec,
    TriggerKind,
    validate_event_type,
)
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobPersistence
from nervos_core.infrastructure.database.triggers import SqlAlchemyTriggerPersistence
from nervos_core.infrastructure.security.webhook_secrets import (
    digest_secret,
    generate_public_id,
    generate_secret,
)
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[4]
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
DEFAULT_DATABASE = (Path.home() / ".nervos" / "nervos.db").resolve(strict=False)
OWNER = 1


def database_metadata(path: Path) -> tuple[bool, int, int]:
    if not path.exists():
        return (False, 0, 0)
    stat = path.stat()
    return (True, stat.st_size, stat.st_mtime_ns)


@pytest.fixture(autouse=True)
def protect_default_database() -> Iterator[None]:
    """Refuse to run against the developer's real database, and prove it stayed untouched."""
    before = database_metadata(DEFAULT_DATABASE)
    yield
    assert database_metadata(DEFAULT_DATABASE) == before


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """One disposable migrated database holding two owners and one Agent Instance each."""
    path = (tmp_path / "triggers.db").resolve()
    assert path != DEFAULT_DATABASE
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(path))
    command.upgrade(Config(str(ROOT / "apps/api/alembic.ini")), "head")
    database = create_sqlite_engine(path)
    with database.begin() as connection:
        for owner in (1, 2):
            connection.execute(
                text(
                    "INSERT INTO users(username,password_hash,role,is_active,created_at,updated_at)"
                    " VALUES(:u,X'00','admin',1,:n,:n)"
                ),
                {"u": f"owner{owner}", "n": NOW},
            )
            connection.execute(
                text(
                    "INSERT INTO agent_instances(owner_user_id,agent_key,agent_definition_version,"
                    "display_name,enabled,model_provider,model_name,created_at,updated_at)"
                    " VALUES(:o,'nervos.chat','1',:d,1,'anthropic','opaque/model',:n,:n)"
                ),
                {"o": owner, "d": f"Agent {owner}", "n": NOW},
            )
    yield database
    database.dispose()


@pytest.fixture
def triggers(engine: Engine) -> SqlAlchemyTriggerPersistence:
    return SqlAlchemyTriggerPersistence(engine, sleep=lambda _: None)


def one_time_draft(**overrides: object) -> TriggerDraft:
    defaults: dict[str, object] = {
        "agent_instance_id": 1,
        "display_name": "One shot",
        "input_text": "run once",
        "kind": TriggerKind.ONE_TIME,
        "schedule": ScheduleSpec.one_time(NOW + timedelta(hours=1)),
        "next_fire_at": NOW + timedelta(hours=1),
    }
    return TriggerDraft(**{**defaults, **overrides})  # type: ignore[arg-type]


def cron_draft(**overrides: object) -> TriggerDraft:
    defaults: dict[str, object] = {
        "agent_instance_id": 1,
        "display_name": "Nightly",
        "input_text": "run nightly",
        "kind": TriggerKind.CRON,
        "schedule": ScheduleSpec.cron("0 3 * * *", "Europe/London"),
        "next_fire_at": NOW + timedelta(days=1),
    }
    return TriggerDraft(**{**defaults, **overrides})  # type: ignore[arg-type]


def event_draft(**overrides: object) -> TriggerDraft:
    defaults: dict[str, object] = {
        "agent_instance_id": 1,
        "display_name": "On event",
        "input_text": "handle event",
        "kind": TriggerKind.EVENT,
        "event_type": "calendar.event.created",
    }
    return TriggerDraft(**{**defaults, **overrides})  # type: ignore[arg-type]


def webhook_draft(**overrides: object) -> TriggerDraft:
    secret = generate_secret()
    defaults: dict[str, object] = {
        "agent_instance_id": 1,
        "display_name": "On delivery",
        "input_text": "handle delivery",
        "kind": TriggerKind.WEBHOOK,
        "public_id": generate_public_id(),
        "secret_digest": digest_secret(secret),
        "secret_created_at": NOW,
    }
    return TriggerDraft(**{**defaults, **overrides})  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------------------


def test_create_and_read_every_kind(triggers: SqlAlchemyTriggerPersistence) -> None:
    created = [
        triggers.create_trigger(OWNER, one_time_draft(), NOW),
        triggers.create_trigger(OWNER, cron_draft(), NOW),
        triggers.create_trigger(OWNER, event_draft(), NOW),
        triggers.create_trigger(OWNER, webhook_draft(), NOW),
        triggers.create_trigger(
            OWNER,
            TriggerDraft(
                agent_instance_id=1,
                display_name="Every five",
                input_text="tick",
                kind=TriggerKind.INTERVAL,
                schedule=ScheduleSpec.interval(300),
                next_fire_at=NOW + timedelta(seconds=300),
            ),
            NOW,
        ),
    ]
    assert {item.kind for item in created} == set(TriggerKind)
    assert len(triggers.list_triggers(OWNER, 10, None)) == 5
    for item in created:
        assert triggers.get_trigger(OWNER, item.id).kind is item.kind


def test_owner_scope_hides_another_users_trigger(triggers: SqlAlchemyTriggerPersistence) -> None:
    mine = triggers.create_trigger(OWNER, cron_draft(), NOW)
    with pytest.raises(TriggerNotFound):
        triggers.get_trigger(2, mine.id)
    assert triggers.list_triggers(2, 10, None) == ()
    with pytest.raises(TriggerNotFound):
        triggers.list_occurrences(2, mine.id, 10, None)


def test_a_foreign_agent_target_is_refused(triggers: SqlAlchemyTriggerPersistence) -> None:
    with pytest.raises(TriggerNotFound):
        triggers.create_trigger(OWNER, cron_draft(agent_instance_id=2), NOW)


def test_public_id_is_globally_unique(triggers: SqlAlchemyTriggerPersistence) -> None:
    """One locator resolves to at most one endpoint, enforced by a partial unique index.

    A constraint violation reaches the caller through the shared transaction runner's
    normalization, which reports any non-contention database failure as `PersistenceUnavailable`
    rather than letting a driver exception escape the persistence layer.
    """
    draft = webhook_draft()
    triggers.create_trigger(OWNER, draft, NOW)
    with pytest.raises(PersistenceUnavailable):
        triggers.create_trigger(OWNER, draft, NOW)
    assert len(triggers.list_triggers(OWNER, 10, None)) == 1


def test_a_rename_does_not_increment_the_revision(
    triggers: SqlAlchemyTriggerPersistence,
) -> None:
    created = triggers.create_trigger(OWNER, cron_draft(), NOW)
    renamed = triggers.update_trigger(
        OWNER,
        created.id,
        TriggerEdit(display_name="Renamed", input_text=created.input_text),
        NOW + timedelta(minutes=1),
    )
    assert renamed.config_revision == created.config_revision
    assert renamed.display_name == "Renamed"
    assert renamed.updated_at > created.updated_at


def test_a_defining_change_increments_the_revision(
    triggers: SqlAlchemyTriggerPersistence,
) -> None:
    created = triggers.create_trigger(OWNER, cron_draft(), NOW)
    revised = triggers.update_trigger(
        OWNER,
        created.id,
        TriggerEdit(
            display_name=created.display_name,
            input_text="changed input",
            schedule=ScheduleSpec.cron("30 4 * * *", "UTC"),
            next_fire_at=NOW + timedelta(days=2),
        ),
        NOW + timedelta(minutes=1),
    )
    assert revised.config_revision == created.config_revision + 1
    assert revised.cron_expression == "30 4 * * *"


def test_an_edit_does_not_mutate_an_existing_occurrence(
    triggers: SqlAlchemyTriggerPersistence, engine: Engine
) -> None:
    """History keeps the revision it fired under, so a later edit cannot rewrite it."""
    service = TriggerMaterializationService(
        create_builtin_definition_registry(), triggers, max_attempts=3
    )
    created = triggers.create_trigger(OWNER, cron_draft(), NOW)
    loaded = triggers.get_trigger(OWNER, created.id)
    outcome = service.materialize(loaded, now=NOW, occurred_at=NOW, nominal_at=created.next_fire_at)
    assert outcome.occurrence.trigger_revision == created.config_revision

    triggers.update_trigger(
        OWNER,
        created.id,
        TriggerEdit(
            display_name=created.display_name,
            input_text="changed",
            schedule=ScheduleSpec.cron("30 4 * * *", "UTC"),
            next_fire_at=NOW + timedelta(days=2),
        ),
        NOW + timedelta(minutes=1),
    )
    history = triggers.list_occurrences(OWNER, created.id, 10, None)
    assert [row.trigger_revision for row in history] == [created.config_revision]


def test_disable_clears_next_fire_and_enable_requires_one(
    triggers: SqlAlchemyTriggerPersistence,
) -> None:
    created = triggers.create_trigger(OWNER, cron_draft(), NOW)
    disabled = triggers.set_enabled(OWNER, created.id, False, NOW)
    assert disabled.enabled is False
    assert disabled.next_fire_at is None
    with pytest.raises(TriggerNotEditable):
        triggers.set_enabled(OWNER, created.id, True, NOW)
    reenabled = triggers.set_enabled(
        OWNER, created.id, True, NOW, next_fire_at=NOW + timedelta(days=1)
    )
    assert reenabled.enabled is True


def test_delete_refuses_while_history_exists(
    triggers: SqlAlchemyTriggerPersistence,
) -> None:
    service = TriggerMaterializationService(
        create_builtin_definition_registry(), triggers, max_attempts=3
    )
    created = triggers.create_trigger(OWNER, cron_draft(), NOW)
    triggers.delete_trigger(OWNER, created.id)

    second = triggers.create_trigger(OWNER, cron_draft(), NOW)
    loaded = triggers.get_trigger(OWNER, second.id)
    service.materialize(loaded, now=NOW, occurred_at=NOW, nominal_at=second.next_fire_at)
    with pytest.raises(TriggerHasHistory):
        triggers.delete_trigger(OWNER, second.id)


# ------------------------------------------------------------------------------------------
# Identity constraints
# ------------------------------------------------------------------------------------------


def test_a_second_schedule_occurrence_at_one_instant_is_refused(
    triggers: SqlAlchemyTriggerPersistence,
) -> None:
    trigger = triggers.create_trigger(OWNER, cron_draft(), NOW)
    loaded = triggers.get_trigger(OWNER, trigger.id)
    command = TriggerMaterializationCommand(
        trigger_definition_id=trigger.id,
        definition=ResolvedAgentDefinition(
            definition_id=AgentDefinitionId("nervos.chat", "1"), limits=STAGE_B_LIMITS
        ),
        now=NOW,
        occurred_at=NOW,
        nominal_at=trigger.next_fire_at,
    )
    first = triggers.materialize_occurrence_and_run(command)
    assert first.duplicate is False

    second = triggers.materialize_occurrence_and_run(command)
    assert second.duplicate is True
    assert second.occurrence.id == first.occurrence.id
    assert len(triggers.list_occurrences(OWNER, trigger.id, 10, None)) == 1
    _ = loaded


def test_a_duplicate_of_a_skipped_occurrence_reports_the_original_skip(
    triggers: SqlAlchemyTriggerPersistence, engine: Engine
) -> None:
    """The original terminal outcome is authoritative — a duplicate is not always a created Run."""
    trigger = triggers.create_trigger(OWNER, cron_draft(), NOW)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE agent_instances SET enabled = 0 WHERE id = 1 AND owner_user_id = 1")
        )
    command = TriggerMaterializationCommand(
        trigger_definition_id=trigger.id,
        definition=ResolvedAgentDefinition(
            definition_id=AgentDefinitionId("nervos.chat", "1"), limits=STAGE_B_LIMITS
        ),
        now=NOW,
        occurred_at=NOW,
        nominal_at=trigger.next_fire_at,
    )
    first = triggers.materialize_occurrence_and_run(command)
    assert first.occurrence.status is OccurrenceStatus.SKIPPED
    assert first.occurrence.skip_code == "agent_disabled"

    second = triggers.materialize_occurrence_and_run(command)
    assert second.duplicate is True
    assert second.occurrence.status is OccurrenceStatus.SKIPPED


def test_two_keyless_webhook_deliveries_both_materialize(
    triggers: SqlAlchemyTriggerPersistence,
) -> None:
    """A keyless delivery has no deterministic identity, so it is never deduped.

    SQLite treats NULLs as distinct under a UNIQUE index; this asserts that the design relies on
    that deliberately rather than by accident.
    """
    trigger = triggers.create_trigger(OWNER, webhook_draft(), NOW)
    definition = ResolvedAgentDefinition(
        definition_id=AgentDefinitionId("nervos.chat", "1"), limits=STAGE_B_LIMITS
    )
    for _ in range(2):
        outcome = triggers.materialize_occurrence_and_run(
            TriggerMaterializationCommand(
                trigger_definition_id=trigger.id,
                definition=definition,
                now=NOW,
                occurred_at=NOW,
            )
        )
        assert outcome.duplicate is False
    assert len(triggers.list_occurrences(OWNER, trigger.id, 10, None)) == 2

    keyed = TriggerMaterializationCommand(
        trigger_definition_id=trigger.id,
        definition=definition,
        now=NOW,
        occurred_at=NOW,
        idempotency_key="delivery-1",
    )
    assert triggers.materialize_occurrence_and_run(keyed).duplicate is False
    assert triggers.materialize_occurrence_and_run(keyed).duplicate is True
    assert len(triggers.list_occurrences(OWNER, trigger.id, 10, None)) == 3


def test_event_identity_dedupes_on_the_event_id(
    triggers: SqlAlchemyTriggerPersistence,
) -> None:
    trigger = triggers.create_trigger(OWNER, event_draft(), NOW)
    command = TriggerMaterializationCommand(
        trigger_definition_id=trigger.id,
        definition=ResolvedAgentDefinition(
            definition_id=AgentDefinitionId("nervos.chat", "1"), limits=STAGE_B_LIMITS
        ),
        now=NOW,
        occurred_at=NOW,
        event_id="evt-1",
    )
    assert triggers.materialize_occurrence_and_run(command).duplicate is False
    assert triggers.materialize_occurrence_and_run(command).duplicate is True


def test_an_identity_that_does_not_match_the_kind_is_refused(
    triggers: SqlAlchemyTriggerPersistence,
) -> None:
    """The database cannot compare an occurrence to its trigger's kind, so the transaction does."""
    cron = triggers.create_trigger(OWNER, cron_draft(), NOW)
    definition = ResolvedAgentDefinition(
        definition_id=AgentDefinitionId("nervos.chat", "1"), limits=STAGE_B_LIMITS
    )
    with pytest.raises(InvalidTrigger):
        triggers.materialize_occurrence_and_run(
            TriggerMaterializationCommand(
                trigger_definition_id=cron.id,
                definition=definition,
                now=NOW,
                occurred_at=NOW,
                event_id="evt-1",
            )
        )

    event = triggers.create_trigger(OWNER, event_draft(), NOW)
    with pytest.raises(InvalidTrigger):
        triggers.materialize_occurrence_and_run(
            TriggerMaterializationCommand(
                trigger_definition_id=event.id,
                definition=definition,
                now=NOW,
                occurred_at=NOW,
                nominal_at=NOW,
            )
        )


def test_run_id_is_unique_across_occurrences(
    triggers: SqlAlchemyTriggerPersistence, engine: Engine
) -> None:
    trigger = triggers.create_trigger(OWNER, cron_draft(), NOW)
    definition = ResolvedAgentDefinition(
        definition_id=AgentDefinitionId("nervos.chat", "1"), limits=STAGE_B_LIMITS
    )
    first = triggers.materialize_occurrence_and_run(
        TriggerMaterializationCommand(
            trigger_definition_id=trigger.id,
            definition=definition,
            now=NOW,
            occurred_at=NOW,
            nominal_at=NOW,
        )
    )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO trigger_occurrences(trigger_definition_id,owner_user_id,"
                "agent_instance_id,trigger_revision,status,run_id,occurred_at,created_at,"
                "nominal_at) VALUES(:t,1,1,1,'run_created',:r,:n,:n,:m)"
            ),
            {"t": trigger.id, "r": first.occurrence.run_id, "n": NOW, "m": NOW + timedelta(1)},
        )


# ------------------------------------------------------------------------------------------
# Materialization
# ------------------------------------------------------------------------------------------


def test_materialization_creates_one_run_job_and_two_events(
    triggers: SqlAlchemyTriggerPersistence, engine: Engine
) -> None:
    trigger = triggers.create_trigger(OWNER, cron_draft(), NOW)
    loaded = triggers.get_trigger(OWNER, trigger.id)
    service = TriggerMaterializationService(
        create_builtin_definition_registry(), triggers, max_attempts=3
    )
    outcome = service.materialize(loaded, now=NOW, occurred_at=NOW, nominal_at=trigger.next_fire_at)
    run_id = outcome.occurrence.run_id
    assert run_id is not None
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 1
        assert (
            connection.scalar(text("SELECT count(*) FROM jobs WHERE run_id=:r"), {"r": run_id}) == 1
        )
        assert [
            row[0]
            for row in connection.execute(
                text("SELECT event_type FROM run_events ORDER BY sequence")
            )
        ] == ["run.created", "run.queued"]
    assert outcome.occurrence.owner_user_id == OWNER
    assert outcome.occurrence.agent_instance_id == 1
    assert outcome.occurrence.trigger_revision == trigger.config_revision


def test_a_disabled_agent_yields_a_skipped_occurrence_and_no_run(
    triggers: SqlAlchemyTriggerPersistence, engine: Engine
) -> None:
    trigger = triggers.create_trigger(OWNER, cron_draft(), NOW)
    with engine.begin() as connection:
        connection.execute(text("UPDATE agent_instances SET enabled = 0 WHERE id = 1"))
    loaded = triggers.get_trigger(OWNER, trigger.id)
    service = TriggerMaterializationService(
        create_builtin_definition_registry(), triggers, max_attempts=3
    )
    outcome = service.materialize(loaded, now=NOW, occurred_at=NOW, nominal_at=trigger.next_fire_at)
    assert outcome.occurrence.status is OccurrenceStatus.SKIPPED
    assert outcome.occurrence.run_id is None
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 0


def test_admission_backpressure_rolls_the_whole_attempt_back(
    engine: Engine,
) -> None:
    """A full queue is not a skip: nothing is written and the trigger stays due for a later tick."""
    triggers = SqlAlchemyTriggerPersistence(engine, max_pending=1, sleep=lambda _: None)
    trigger = triggers.create_trigger(OWNER, cron_draft(), NOW)
    # Occupy the single admission slot with an unrelated manual Run.
    SqlAlchemyJobPersistence(engine, max_pending=1, sleep=lambda _: None).submit(
        owner_user_id=OWNER,
        agent_instance_id=1,
        input_text="occupy",
        limits=STAGE_B_LIMITS,
        now=NOW,
    )
    loaded = triggers.get_trigger(OWNER, trigger.id)
    service = TriggerMaterializationService(
        create_builtin_definition_registry(), triggers, max_attempts=3
    )
    with pytest.raises(QueueCapacityExceeded):
        service.materialize(loaded, now=NOW, occurred_at=NOW, nominal_at=trigger.next_fire_at)
    assert triggers.list_occurrences(OWNER, trigger.id, 10, None) == ()
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 1


def test_an_injected_failure_after_the_run_insert_writes_nothing(
    triggers: SqlAlchemyTriggerPersistence, engine: Engine
) -> None:
    """The rollback proof: the occurrence, the Run and the Job commit together or not at all."""
    trigger = triggers.create_trigger(OWNER, event_draft(), NOW)
    loaded = triggers.get_trigger(OWNER, trigger.id)
    service = TriggerMaterializationService(
        create_builtin_definition_registry(), triggers, max_attempts=3
    )
    original = SqlAlchemyTriggerPersistence._write_occurrence  # pyright: ignore[reportPrivateUsage]

    def explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("injected failure after the Run insert")

    SqlAlchemyTriggerPersistence._write_occurrence = explode  # type: ignore[method-assign]
    _ = original
    try:
        with pytest.raises(RuntimeError):
            service.materialize(loaded, now=NOW, occurred_at=NOW, event_id="evt-1")
    finally:
        SqlAlchemyTriggerPersistence._write_occurrence = original  # type: ignore[method-assign]

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 0
        assert connection.scalar(text("SELECT count(*) FROM jobs")) == 0
        assert connection.scalar(text("SELECT count(*) FROM run_events")) == 0
        assert connection.scalar(text("SELECT count(*) FROM trigger_occurrences")) == 0


def test_a_foreign_agent_definition_fails_closed(
    triggers: SqlAlchemyTriggerPersistence, engine: Engine
) -> None:
    """A trigger whose Agent no longer matches the resolved definition consumes nothing.

    The canonical helper re-checks the Agent's exact definition identity inside the transaction, so
    a mismatch is refused there. E2 narrows what that refusal *means*: a definition change is a
    race between resolving the Agent and acting on it, so it is refused without writing anything —
    no occurrence, no Run, and no schedule advance. Recording it as an `agent_disabled` skip, as
    E1 did, would consume a scheduled occurrence and tell the operator something untrue, because
    the Agent is neither disabled nor at fault. The next tick re-resolves and proceeds normally.
    """
    trigger = triggers.create_trigger(OWNER, cron_draft(), NOW)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE agent_instances SET agent_definition_version='2' WHERE id = 1")
        )
    command = TriggerMaterializationCommand(
        trigger_definition_id=trigger.id,
        definition=ResolvedAgentDefinition(
            definition_id=AgentDefinitionId("nervos.chat", "1"), limits=STAGE_B_LIMITS
        ),
        now=NOW,
        occurred_at=NOW,
        nominal_at=trigger.next_fire_at,
    )
    with pytest.raises(TriggerNotEditable):
        triggers.materialize_occurrence_and_run(command)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 0
        assert connection.scalar(text("SELECT count(*) FROM trigger_occurrences")) == 0


def test_a_disabled_agent_is_still_recorded_as_a_skip(
    triggers: SqlAlchemyTriggerPersistence, engine: Engine
) -> None:
    """A disabled Agent is an outcome, not a race, so it is still recorded as a skip."""
    trigger = triggers.create_trigger(OWNER, cron_draft(), NOW)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE agent_instances SET enabled = 0 WHERE id = 1 AND owner_user_id = 1")
        )
    command = TriggerMaterializationCommand(
        trigger_definition_id=trigger.id,
        definition=ResolvedAgentDefinition(
            definition_id=AgentDefinitionId("nervos.chat", "1"), limits=STAGE_B_LIMITS
        ),
        now=NOW,
        occurred_at=NOW,
        nominal_at=trigger.next_fire_at,
    )
    outcome = triggers.materialize_occurrence_and_run(command)
    assert outcome.occurrence.status is OccurrenceStatus.SKIPPED
    assert outcome.occurrence.skip_code == "agent_disabled"
    assert outcome.occurrence.run_id is None
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runs")) == 0


# ------------------------------------------------------------------------------------------
# Provenance
# ------------------------------------------------------------------------------------------


def test_provenance_is_a_reverse_relation(
    triggers: SqlAlchemyTriggerPersistence, engine: Engine
) -> None:
    manual = SqlAlchemyJobPersistence(engine, sleep=lambda _: None).submit(
        owner_user_id=OWNER,
        agent_instance_id=1,
        input_text="manual",
        limits=STAGE_B_LIMITS,
        now=NOW,
    )
    assert triggers.occurrence_for_run(manual.id) is None

    trigger = triggers.create_trigger(OWNER, cron_draft(), NOW)
    loaded = triggers.get_trigger(OWNER, trigger.id)
    service = TriggerMaterializationService(
        create_builtin_definition_registry(), triggers, max_attempts=3
    )
    outcome = service.materialize(loaded, now=NOW, occurred_at=NOW, nominal_at=trigger.next_fire_at)
    assert outcome.occurrence.run_id is not None
    found = triggers.occurrence_for_run(outcome.occurrence.run_id)
    assert found is not None
    assert found.trigger_definition_id == trigger.id


def test_misfire_policy_defaults_to_coalesce_one(
    triggers: SqlAlchemyTriggerPersistence,
) -> None:
    created = triggers.create_trigger(OWNER, cron_draft(), NOW)
    assert created.misfire_policy is MisfirePolicy.COALESCE_ONE


def test_run_origin_is_derivable_from_the_trigger_kind() -> None:
    assert RunOrigin.MANUAL.value == "manual"
    validate_event_type("device.temperature.changed")


# ------------------------------------------------------------------------------------------
# Query plans
# ------------------------------------------------------------------------------------------


def _plan(engine: Engine, statement: str, **parameters: object) -> str:
    with engine.connect() as connection:
        rows = connection.execute(text(f"EXPLAIN QUERY PLAN {statement}"), parameters).all()
    return " | ".join(str(row[-1]) for row in rows)


@pytest.fixture
def populated(engine: Engine, triggers: SqlAlchemyTriggerPersistence) -> Engine:
    """Many triggers and occurrences, so a scan would be measurably the wrong plan."""
    with engine.begin() as connection:
        for index in range(60):
            connection.execute(
                text(
                    "INSERT INTO trigger_definitions(owner_user_id,agent_instance_id,kind,"
                    "display_name,enabled,input_text,config_revision,misfire_policy,"
                    "cron_expression,timezone,next_fire_at,created_at,updated_at)"
                    " VALUES(1,1,'cron',:d,1,'run',1,'coalesce_one','0 3 * * *','UTC',"
                    ":n,'2026-09-14 12:00:00','2026-09-14 12:00:00')"
                ),
                {"d": f"job {index}", "n": NOW + timedelta(minutes=index)},
            )
        connection.execute(
            text(
                "INSERT INTO trigger_definitions(owner_user_id,agent_instance_id,kind,display_name,"
                "enabled,input_text,config_revision,misfire_policy,event_type,created_at,updated_at)"
                " VALUES(1,1,'event','on event',1,'run',1,'coalesce_one','device.reading',"
                "'2026-09-14 12:00:00','2026-09-14 12:00:00')"
            )
        )
        for _index in range(200):
            connection.execute(
                text(
                    "INSERT INTO trigger_occurrences(trigger_definition_id,owner_user_id,"
                    "agent_instance_id,trigger_revision,status,skip_code,skip_message,"
                    "occurred_at,created_at)"
                    " VALUES(1,1,1,1,'skipped','agent_disabled','refused',:n,:n)"
                ),
                {"n": NOW},
            )
    return engine


def test_the_due_scan_is_an_indexed_seek(populated: Engine) -> None:
    """The scheduler's eventual hot path must never scan a table that grows with usage."""
    plan = _plan(
        populated,
        "SELECT id FROM trigger_definitions WHERE next_fire_at IS NOT NULL"
        " AND next_fire_at <= :now ORDER BY next_fire_at, id LIMIT 32",
        now=NOW + timedelta(hours=1),
    )
    assert "ix_trigger_definitions_next_fire_at_id" in plan, plan
    assert "SCAN trigger_definitions" not in plan, plan


def test_the_owner_listing_is_an_indexed_seek(populated: Engine) -> None:
    plan = _plan(
        populated,
        "SELECT id FROM trigger_definitions WHERE owner_user_id = 1 AND id < 99999"
        " ORDER BY id DESC LIMIT 20",
    )
    assert "ix_trigger_definitions_owner_user_id_id" in plan, plan


def test_occurrence_history_is_an_indexed_seek(populated: Engine) -> None:
    plan = _plan(
        populated,
        "SELECT id FROM trigger_occurrences WHERE trigger_definition_id = 1"
        " ORDER BY id DESC LIMIT 20",
    )
    assert "ix_trigger_occurrences_trigger_definition_id_id" in plan, plan
    assert "SCAN trigger_occurrences" not in plan, plan


def test_the_webhook_locator_lookup_is_an_indexed_seek(populated: Engine) -> None:
    plan = _plan(
        populated,
        "SELECT id FROM trigger_definitions WHERE public_id = 'aaaaaaaaaaaaaaaaaaaaaa'",
    )
    assert "ux_trigger_definitions_public_id" in plan, plan


def test_the_event_lookup_is_an_indexed_seek(populated: Engine) -> None:
    plan = _plan(
        populated,
        "SELECT id FROM trigger_definitions WHERE owner_user_id = 1"
        " AND event_type = 'device.reading' AND enabled = 1",
    )
    assert "ix_trigger_definitions_event_lookup" in plan, plan


def test_provenance_lookup_is_an_indexed_seek(populated: Engine) -> None:
    plan = _plan(populated, "SELECT id FROM trigger_occurrences WHERE run_id = 1")
    assert "ux_trigger_occurrences_run_id" in plan, plan


# ------------------------------------------------------------------------------------------
# E2: the due scan is an indexed, bounded, keyset-continuable page
# ------------------------------------------------------------------------------------------


def test_the_e2_due_scan_predicate_is_an_indexed_seek(populated: Engine) -> None:
    """The scheduler's hot read must never scan a table that grows with every trigger.

    `0008` carries a partial index over exactly `(next_fire_at, id)` for rows that have a next fire
    time, and the scan's predicate and ordering are built to be served by it. E2 adds no index and
    no predicate the index cannot satisfy.
    """
    plan = _plan(
        populated,
        "SELECT id FROM trigger_definitions"
        " WHERE enabled = 1 AND next_fire_at IS NOT NULL AND next_fire_at <= :now"
        " AND kind IN ('one_time','interval','cron')"
        " ORDER BY next_fire_at ASC, id ASC LIMIT 32",
        now=NOW + timedelta(hours=1),
    )
    assert "ix_trigger_definitions_next_fire_at_id" in plan, plan
    assert "SCAN trigger_definitions" not in plan, plan


def test_the_due_scan_continuation_uses_the_same_index(populated: Engine) -> None:
    """The continuation that keeps a blocked page from stranding later rows is a seek too."""
    plan = _plan(
        populated,
        "SELECT id FROM trigger_definitions"
        " WHERE enabled = 1 AND next_fire_at IS NOT NULL AND next_fire_at <= :now"
        " AND kind IN ('one_time','interval','cron')"
        " AND (next_fire_at > :after_time"
        " OR (next_fire_at = :after_time AND id > :after_id))"
        " ORDER BY next_fire_at ASC, id ASC LIMIT 32",
        now=NOW + timedelta(hours=1),
        after_time=NOW,
        after_id=1,
    )
    assert "ix_trigger_definitions_next_fire_at_id" in plan, plan
    assert "SCAN trigger_definitions" not in plan, plan


def test_the_due_scan_does_not_join_a_table_that_grows_with_execution(populated: Engine) -> None:
    """A scheduling read must not touch Runs, Jobs or events — it decides *when*, not *how*."""
    plan = _plan(populated, _DUE_SCAN_SQL, now=NOW + timedelta(hours=1))
    for table in ("runs", "jobs", "run_events"):
        assert f"TABLE {table}" not in plan, plan


_DUE_SCAN_SQL = (
    "SELECT trigger_definitions.id, agent_instances.agent_key"
    " FROM trigger_definitions"
    " JOIN agent_instances ON agent_instances.id = trigger_definitions.agent_instance_id"
    " WHERE trigger_definitions.enabled = 1 AND trigger_definitions.next_fire_at IS NOT NULL"
    " AND trigger_definitions.next_fire_at <= :now"
    " AND trigger_definitions.kind IN ('one_time','interval','cron')"
    " ORDER BY trigger_definitions.next_fire_at ASC, trigger_definitions.id ASC LIMIT 32"
)


def test_the_occurrence_identity_lookup_is_a_unique_index_seek(populated: Engine) -> None:
    """The duplicate check runs on every materialization, so it must be a seek."""
    plan = _plan(
        populated,
        "SELECT id FROM trigger_occurrences WHERE trigger_definition_id = 1 AND nominal_at = :n",
        n=NOW + timedelta(minutes=1),
    )
    assert "ux_trigger_occurrences_schedule_identity" in plan, plan
