"""E4 trigger management: schedule semantics, edit concurrency, lifecycle, and isolation.

The management service is driven over a disposable migrated database, so every assertion is about
a durable outcome rather than a fake's opinion. All instants are parameters: a schedule becomes due
by *storing* an instant, and a race is produced by presenting a state another writer has already
moved -- never by sleeping.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from nervos_core.application.triggers import (
    TriggerConfigConflict,
    TriggerHasHistory,
    TriggerManagementService,
    TriggerNotEditable,
    TriggerNotFound,
)
from nervos_core.application.webhooks import (
    WebhookProvisioningService,
    WebhookSecretService,
)
from nervos_core.domain.triggers import ScheduleSpec, TriggerKind
from nervos_core.infrastructure.database.triggers import SqlAlchemyTriggerPersistence
from nervos_core.infrastructure.scheduling import create_schedule_evaluator
from nervos_core.infrastructure.webhooks import RandomWebhookCredentialFactory
from scheduler_support import AGENT, NOW, OTHER_OWNER, OWNER, migrate
from sqlalchemy import Engine, text

INTERVAL = 300
ONCE = NOW + timedelta(hours=1)
PAST = NOW - timedelta(hours=1)


class Rig:
    """One migrated database and the composed E4 management surface."""

    def __init__(self, engine: Engine, path: Path) -> None:
        self.engine = engine
        self.path = path
        self.triggers = SqlAlchemyTriggerPersistence(engine, sleep=lambda _: None)
        factory = RandomWebhookCredentialFactory()
        self.service = TriggerManagementService(
            self.triggers,
            WebhookProvisioningService(self.triggers, factory),
            WebhookSecretService(self.triggers, factory),
            create_schedule_evaluator(),
            clock=lambda: NOW,
        )

    # ------------------------------------------------------------------------------------------
    # Durable truth
    # ------------------------------------------------------------------------------------------

    def stored(self, trigger_id: int, column: str) -> object:
        with self.engine.connect() as connection:
            return connection.scalar(
                text(f"SELECT {column} FROM trigger_definitions WHERE id = :id"),
                {"id": trigger_id},
            )

    def counts(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            return {
                table: int(connection.scalar(text(f"SELECT COUNT(*) FROM {table}")) or 0)
                for table in ("trigger_definitions", "trigger_occurrences", "runs")
            }

    def add_other_agent(self) -> int:
        """Give the second owner its own Agent Instance, returning its identifier."""
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO agent_instances(owner_user_id,agent_key,"
                    "agent_definition_version,display_name,enabled,model_provider,model_name,"
                    "created_at,updated_at) "
                    "VALUES(:owner,'nervos.chat','2','Other Agent',1,'anthropic',"
                    "'opaque/model',:now,:now)"
                ),
                {"owner": OTHER_OWNER, "now": NOW},
            )
            return int(
                connection.scalar(
                    text(
                        "SELECT id FROM agent_instances WHERE owner_user_id = :owner "
                        "ORDER BY id DESC LIMIT 1"
                    ),
                    {"owner": OTHER_OWNER},
                )
                or 0
            )


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    built = Rig(migrate(tmp_path / "nervos.db", monkeypatch), tmp_path)
    try:
        yield built
    finally:
        built.engine.dispose()


def one_time(rig: Rig, *, enabled: bool = True, run_at: datetime = ONCE):
    return rig.service.create_trigger(
        OWNER,
        agent_instance_id=AGENT,
        display_name="One time",
        input_text="run once",
        kind=TriggerKind.ONE_TIME,
        schedule=ScheduleSpec.one_time(run_at),
        enabled=enabled,
    )


def _record_one_occurrence(rig: Rig, trigger_id: int) -> None:
    """Give one trigger a real Run and a real run_created occurrence.

    Written through the same tables the materializer writes, so "this trigger has history" is the
    durable fact the delete and completion rules actually consult, not a stub.
    """
    with rig.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runs(agent_instance_id,status,agent_key,agent_definition_version,"
                "model_provider,model_name,input_text,input_max_bytes,input_max_code_points,"
                "output_max_bytes,output_max_code_points,provider_timeout_ms,max_output_tokens,"
                "max_model_calls,max_tool_calls,tool_timeout_ms,tool_result_max_bytes,"
                "max_consecutive_tool_failures,tool_grant_cutoff_id,created_at)"
                " VALUES(:agent,'created','nervos.chat','2','anthropic','opaque/model','x',"
                "8000,4000,32000,16000,60000,1024,8,8,30000,65536,3,0,:now)"
            ),
            {"agent": AGENT, "now": NOW},
        )
        run_id = int(connection.scalar(text("SELECT MAX(id) FROM runs")) or 0)
        connection.execute(
            text(
                "INSERT INTO trigger_occurrences(trigger_definition_id,owner_user_id,"
                "agent_instance_id,trigger_revision,status,run_id,occurred_at,created_at)"
                " VALUES(:trigger,1,:agent,1,'run_created',:run,:now,:now)"
            ),
            {"trigger": trigger_id, "agent": AGENT, "run": run_id, "now": NOW},
        )


# ------------------------------------------------------------------------------------------------
# Creation: the requested enabled state is atomic, and the schedule state follows it
# ------------------------------------------------------------------------------------------------


def test_an_enabled_one_time_trigger_stores_its_own_instant(rig: Rig) -> None:
    trigger = one_time(rig)

    assert trigger.enabled is True
    assert trigger.next_fire_at == ONCE
    assert trigger.config_revision == 1


def test_a_one_time_instant_already_in_the_past_is_due_immediately(rig: Rig) -> None:
    """The named instant is stored, not shifted, so an overdue schedule is simply due."""
    trigger = one_time(rig, run_at=PAST)
    assert trigger.next_fire_at == PAST


def test_a_disabled_one_time_trigger_stores_no_next_fire(rig: Rig) -> None:
    trigger = one_time(rig, enabled=False)

    assert trigger.enabled is False
    assert trigger.next_fire_at is None
    # It is created disabled in one transaction: there is no instant at which it was enabled.
    assert rig.stored(trigger.id, "enabled") == 0


def test_an_enabled_interval_starts_one_whole_period_from_now(rig: Rig) -> None:
    trigger = rig.service.create_trigger(
        OWNER,
        agent_instance_id=AGENT,
        display_name="Interval",
        input_text="poll",
        kind=TriggerKind.INTERVAL,
        schedule=ScheduleSpec.interval(INTERVAL),
    )
    assert trigger.next_fire_at == NOW + timedelta(seconds=INTERVAL)


def test_a_disabled_interval_stores_no_next_fire(rig: Rig) -> None:
    trigger = rig.service.create_trigger(
        OWNER,
        agent_instance_id=AGENT,
        display_name="Interval",
        input_text="poll",
        kind=TriggerKind.INTERVAL,
        schedule=ScheduleSpec.interval(INTERVAL),
        enabled=False,
    )
    assert trigger.next_fire_at is None


def test_an_enabled_cron_uses_the_evaluator_for_its_first_future_instant(rig: Rig) -> None:
    """A cron created at 12:00 with a 09:00 daily expression fires tomorrow, not immediately."""
    trigger = rig.service.create_trigger(
        OWNER,
        agent_instance_id=AGENT,
        display_name="Cron",
        input_text="daily",
        kind=TriggerKind.CRON,
        schedule=ScheduleSpec.cron("0 9 * * *", "UTC"),
    )
    assert trigger.next_fire_at == NOW + timedelta(hours=21)


def test_an_event_trigger_carries_its_type_and_no_schedule(rig: Rig) -> None:
    trigger = rig.service.create_trigger(
        OWNER,
        agent_instance_id=AGENT,
        display_name="On reading",
        input_text="react",
        kind=TriggerKind.EVENT,
        event_type="device.reading",
    )
    assert trigger.event_type == "device.reading"
    assert trigger.next_fire_at is None


def test_a_webhook_trigger_created_disabled_still_gets_its_locator_and_secret(rig: Rig) -> None:
    issued = rig.service.create_webhook(
        OWNER,
        agent_instance_id=AGENT,
        display_name="Hook",
        input_text="handle",
        enabled=False,
    )
    assert issued.trigger.enabled is False
    assert issued.trigger.public_id is not None
    assert len(issued.secret) == 43
    assert issued.trigger.secret_digest is not None


# ------------------------------------------------------------------------------------------------
# Editing: a rename is not defining; a defining edit bumps the revision and moves the schedule
# ------------------------------------------------------------------------------------------------


def test_a_rename_does_not_bump_the_revision_or_move_the_next_fire(rig: Rig) -> None:
    trigger = one_time(rig)

    edited = rig.service.update_trigger(
        OWNER, trigger.id, expected_config_revision=1, display_name="Renamed"
    )

    assert edited.display_name == "Renamed"
    assert edited.config_revision == 1
    assert edited.next_fire_at == ONCE


def test_a_defining_edit_bumps_the_revision_and_recomputes_an_enabled_schedule(rig: Rig) -> None:
    trigger = rig.service.create_trigger(
        OWNER,
        agent_instance_id=AGENT,
        display_name="Interval",
        input_text="poll",
        kind=TriggerKind.INTERVAL,
        schedule=ScheduleSpec.interval(INTERVAL),
    )

    edited = rig.service.update_trigger(
        OWNER,
        trigger.id,
        expected_config_revision=1,
        schedule=ScheduleSpec.interval(600),
    )

    assert edited.kind is TriggerKind.INTERVAL
    assert edited.config_revision == 2
    assert edited.interval_seconds == 600
    # An enabled schedule recomputes its first future instant from the *current* clock.
    assert edited.next_fire_at == NOW + timedelta(seconds=600)


def test_a_schedule_spec_of_another_kind_is_refused_by_the_domain(rig: Rig) -> None:
    """An interval trigger cannot be handed a one-time spec: the row's kind shape is enforced."""
    from nervos_core.domain.triggers import InvalidTrigger

    trigger = rig.service.create_trigger(
        OWNER,
        agent_instance_id=AGENT,
        display_name="Interval",
        input_text="poll",
        kind=TriggerKind.INTERVAL,
        schedule=ScheduleSpec.interval(INTERVAL),
    )

    with pytest.raises(InvalidTrigger):
        rig.service.update_trigger(
            OWNER,
            trigger.id,
            expected_config_revision=1,
            schedule=ScheduleSpec.one_time(NOW + timedelta(hours=2)),
        )


def test_a_defining_edit_of_a_disabled_schedule_keeps_the_next_fire_null(rig: Rig) -> None:
    trigger = rig.service.create_trigger(
        OWNER,
        agent_instance_id=AGENT,
        display_name="Interval",
        input_text="poll",
        kind=TriggerKind.INTERVAL,
        schedule=ScheduleSpec.interval(INTERVAL),
        enabled=False,
    )

    edited = rig.service.update_trigger(
        OWNER,
        trigger.id,
        expected_config_revision=1,
        schedule=ScheduleSpec.interval(600),
    )

    assert edited.config_revision == 2
    assert edited.next_fire_at is None
    assert edited.interval_seconds == 600


def test_two_defining_edits_from_one_revision_leave_exactly_one_winner(rig: Rig) -> None:
    """The revision guard is inside the write transaction, so the loser is told, not overwritten."""
    trigger = rig.service.create_trigger(
        OWNER,
        agent_instance_id=AGENT,
        display_name="Interval",
        input_text="poll",
        kind=TriggerKind.INTERVAL,
        schedule=ScheduleSpec.interval(INTERVAL),
    )

    first = rig.service.update_trigger(
        OWNER,
        trigger.id,
        expected_config_revision=1,
        input_text="first writer",
    )
    assert first.config_revision == 2

    with pytest.raises(TriggerConfigConflict):
        rig.service.update_trigger(
            OWNER,
            trigger.id,
            expected_config_revision=1,
            input_text="second writer",
        )

    assert rig.stored(trigger.id, "input_text") == "first writer"
    assert rig.stored(trigger.id, "config_revision") == 2


def test_a_rename_concurrency_is_not_detected_and_the_last_writer_wins(rig: Rig) -> None:
    """The documented limitation, asserted rather than glossed over.

    `config_revision` protects the *defining* configuration, so a display-name-only edit does not
    increment it. Two concurrent renames therefore both pass the revision check and the last
    commit wins -- which is exactly why the revision must never be advertised as a general row
    version, and why defining edits are the ones this guard is for.
    """
    trigger = one_time(rig)
    rig.service.update_trigger(OWNER, trigger.id, expected_config_revision=1, display_name="A")

    second = rig.service.update_trigger(
        OWNER, trigger.id, expected_config_revision=1, display_name="B"
    )

    assert second.display_name == "B"
    assert second.config_revision == 1
    assert rig.stored(trigger.id, "config_revision") == 1


# ------------------------------------------------------------------------------------------------
# Lifecycle: disable clears the instant, enable recomputes the first future one
# ------------------------------------------------------------------------------------------------


def test_disable_clears_the_next_fire_and_enable_recomputes_it(rig: Rig) -> None:
    trigger = rig.service.create_trigger(
        OWNER,
        agent_instance_id=AGENT,
        display_name="Interval",
        input_text="poll",
        kind=TriggerKind.INTERVAL,
        schedule=ScheduleSpec.interval(INTERVAL),
    )

    disabled = rig.service.set_enabled(OWNER, trigger.id, False)
    assert disabled.enabled is False
    assert disabled.next_fire_at is None

    enabled = rig.service.set_enabled(OWNER, trigger.id, True)
    assert enabled.enabled is True
    assert enabled.next_fire_at == NOW + timedelta(seconds=INTERVAL)


def test_disabling_an_event_trigger_keeps_its_type(rig: Rig) -> None:
    trigger = rig.service.create_trigger(
        OWNER,
        agent_instance_id=AGENT,
        display_name="On reading",
        input_text="react",
        kind=TriggerKind.EVENT,
        event_type="device.reading",
    )

    disabled = rig.service.set_enabled(OWNER, trigger.id, False)

    assert disabled.enabled is False
    assert disabled.next_fire_at is None
    assert disabled.event_type == "device.reading"


# ------------------------------------------------------------------------------------------------
# Delete: history is evidence, so a trigger that has any cannot be removed
# ------------------------------------------------------------------------------------------------


def test_a_trigger_without_history_is_deleted(rig: Rig) -> None:
    trigger = one_time(rig)

    rig.service.delete_trigger(OWNER, trigger.id)

    assert rig.counts()["trigger_definitions"] == 0
    with pytest.raises(TriggerNotFound):
        rig.service.get_trigger(OWNER, trigger.id)


def test_a_trigger_with_history_is_not_deleted(rig: Rig) -> None:
    trigger = one_time(rig)
    _record_one_occurrence(rig, trigger.id)

    with pytest.raises(TriggerHasHistory):
        rig.service.delete_trigger(OWNER, trigger.id)

    assert rig.counts()["trigger_definitions"] == 1
    assert rig.counts()["trigger_occurrences"] == 1


def test_a_completed_one_time_trigger_cannot_be_re_enabled(rig: Rig) -> None:
    trigger = one_time(rig, run_at=PAST)
    _record_one_occurrence(rig, trigger.id)

    with pytest.raises(TriggerNotEditable):
        rig.service.set_enabled(OWNER, trigger.id, True)


# ------------------------------------------------------------------------------------------------
# Webhook rotation and owner isolation
# ------------------------------------------------------------------------------------------------


def test_rotation_keeps_the_locator_and_revision_and_replaces_the_credential(rig: Rig) -> None:
    issued = rig.service.create_webhook(
        OWNER, agent_instance_id=AGENT, display_name="Hook", input_text="handle"
    )
    rotated = rig.service.rotate_webhook_secret(OWNER, issued.trigger.id)

    assert rotated.trigger.public_id == issued.trigger.public_id
    assert rotated.trigger.config_revision == issued.trigger.config_revision
    assert rotated.secret != issued.secret
    assert rotated.trigger.secret_created_at is not None
    assert rig.stored(issued.trigger.id, "secret_created_at") is not None


def test_rotating_a_non_webhook_trigger_is_refused(rig: Rig) -> None:
    trigger = one_time(rig)

    with pytest.raises(TriggerNotEditable):
        rig.service.rotate_webhook_secret(OWNER, trigger.id)


def test_every_management_operation_is_owner_scoped(rig: Rig) -> None:
    trigger = one_time(rig)
    assert rig.add_other_agent() > 0

    with pytest.raises(TriggerNotFound):
        rig.service.get_trigger(OTHER_OWNER, trigger.id)
    with pytest.raises(TriggerNotFound):
        rig.service.update_trigger(
            OTHER_OWNER, trigger.id, expected_config_revision=1, display_name="Stolen"
        )
    with pytest.raises(TriggerNotFound):
        rig.service.set_enabled(OTHER_OWNER, trigger.id, False)
    with pytest.raises(TriggerNotFound):
        rig.service.delete_trigger(OTHER_OWNER, trigger.id)
    with pytest.raises(TriggerNotFound):
        rig.service.rotate_webhook_secret(OTHER_OWNER, trigger.id)
    with pytest.raises(TriggerNotFound):
        rig.service.list_occurrences(OTHER_OWNER, trigger.id, limit=20, before_id=None)

    # Nothing moved: the owner's own trigger is exactly as it was, and the other owner sees none.
    assert rig.stored(trigger.id, "display_name") == "One time"
    assert rig.stored(trigger.id, "enabled") == 1
    assert rig.service.list_triggers(OTHER_OWNER, limit=20, before_id=None) == ()


def test_listing_is_newest_first_and_keyset_continuable(rig: Rig) -> None:
    first = one_time(rig)
    second = one_time(rig)

    listed = rig.service.list_triggers(OWNER, limit=20, before_id=None)

    assert [trigger.id for trigger in listed] == [second.id, first.id]
    assert rig.service.list_triggers(OWNER, limit=1, before_id=None)[0].id == second.id
    assert rig.service.list_triggers(OWNER, limit=20, before_id=second.id)[0].id == first.id


def test_e5_management_matrix_keeps_all_five_kinds_owner_scoped(rig: Rig) -> None:
    """The integrated management surface exposes every kind without crossing owners."""
    one_time_trigger = one_time(rig)
    interval_trigger = rig.service.create_trigger(
        OWNER,
        agent_instance_id=AGENT,
        display_name="Interval",
        input_text="interval work",
        kind=TriggerKind.INTERVAL,
        schedule=ScheduleSpec.interval(INTERVAL),
    )
    cron_trigger = rig.service.create_trigger(
        OWNER,
        agent_instance_id=AGENT,
        display_name="Cron",
        input_text="cron work",
        kind=TriggerKind.CRON,
        schedule=ScheduleSpec.cron("0 9 * * *", "UTC"),
    )
    webhook = rig.service.create_webhook(
        OWNER,
        agent_instance_id=AGENT,
        display_name="Webhook",
        input_text="webhook work",
    )
    event_trigger = rig.service.create_trigger(
        OWNER,
        agent_instance_id=AGENT,
        display_name="Event",
        input_text="event work",
        kind=TriggerKind.EVENT,
        event_type="device.reading",
    )

    listed = rig.service.list_triggers(OWNER, limit=10, before_id=None)
    assert {trigger.kind for trigger in listed} == {
        TriggerKind.ONE_TIME,
        TriggerKind.INTERVAL,
        TriggerKind.CRON,
        TriggerKind.WEBHOOK,
        TriggerKind.EVENT,
    }
    assert {trigger.id for trigger in listed} == {
        one_time_trigger.id,
        interval_trigger.id,
        cron_trigger.id,
        webhook.trigger.id,
        event_trigger.id,
    }
    for trigger in listed:
        assert rig.service.get_trigger(OWNER, trigger.id).id == trigger.id

    assert rig.service.list_triggers(OTHER_OWNER, limit=10, before_id=None) == ()
    with pytest.raises(TriggerNotFound):
        rig.service.get_trigger(OTHER_OWNER, event_trigger.id)
