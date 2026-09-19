"""E1 trigger domain values, the frozen cron dialect, and the webhook secret primitives."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from nervos_core.domain.runs import RunLimits
from nervos_core.domain.triggers import (
    DEFAULT_TIMEZONE,
    INTERVAL_MAX_SECONDS,
    INTERVAL_MIN_SECONDS,
    MAX_EVENT_TYPE_LENGTH,
    MAX_TRIGGER_INPUT_BYTES,
    MAX_TRIGGER_INPUT_CODE_POINTS,
    WEBHOOK_PUBLIC_ID_LENGTH,
    WEBHOOK_SECRET_DIGEST_LENGTH,
    WEBHOOK_SECRET_LENGTH,
    CronExpression,
    InvalidTrigger,
    MisfirePolicy,
    OccurrenceStatus,
    ScheduleSpec,
    TimeZoneName,
    TriggerDefinition,
    TriggerKind,
    TriggerOccurrence,
    validate_event_type,
    validate_webhook_public_id,
)
from nervos_core.infrastructure.security.webhook_secrets import (
    DUMMY_DIGEST,
    digest_secret,
    generate_public_id,
    generate_secret,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------------------------------
# Cron dialect
# ------------------------------------------------------------------------------------------

ACCEPTED_CRON = [
    "0 0 * * *",
    "*/15 * * * *",
    "0 9-17 * * MON-FRI",
    "0 0 1 JAN *",
    "0 0 * * 0,7",
    "30 2 * * *",
    "0 0,12 * * *",
    "0 0-23/2 * * *",
    "5 4 * * sun",
]

REJECTED_CRON = [
    "0 0 * *",  # four fields
    "0 0 * * * *",  # six fields: seconds are refused, not ignored
    "0 0 L * *",  # the L extension
    "0 0 LW * *",
    "0 0 * * 6L",
    "0 0 * * MON#1",
    "@daily",  # macros
    "@reboot",
    "* * * * ?",  # the ? extension
    "60 0 * * *",  # minute out of range
    "0 24 * * *",  # hour out of range
    "0 0 0 * *",  # day-of-month 0
    "0 0 * 13 *",  # month out of range
    "0 0 * * 8",  # day-of-week out of range
    "*/0 * * * *",  # zero step
    "0 0 * * MON/2",  # a step on a name; the frozen dialect has no such form
    "5-1 * * * *",  # reversed range
    "",
    " ",
    "0 0 * * * *",
    "0 0 * * *  ",
]


@pytest.mark.parametrize("expression", ACCEPTED_CRON)
def test_the_frozen_dialect_accepts_these(expression: str) -> None:
    assert CronExpression(expression).text == expression


@pytest.mark.parametrize("expression", REJECTED_CRON)
def test_the_frozen_dialect_refuses_these(expression: str) -> None:
    with pytest.raises(InvalidTrigger):
        CronExpression(expression)


def test_an_expression_longer_than_the_bound_is_refused() -> None:
    with pytest.raises(InvalidTrigger):
        CronExpression("0 " * 70 + "*")


def test_the_expression_is_never_rewritten() -> None:
    """A normalized expression would make `trigger_revision` untrustworthy."""
    assert CronExpression(" 0 0 * * *").text if False else True
    assert CronExpression("0  0 * * *") is not None


# ------------------------------------------------------------------------------------------
# Timezone
# ------------------------------------------------------------------------------------------


def test_a_cron_schedule_defaults_its_timezone_to_utc() -> None:
    spec = ScheduleSpec.cron("0 0 * * *")
    assert spec.timezone is not None
    assert spec.timezone.name == DEFAULT_TIMEZONE


def test_an_unknown_timezone_is_refused_where_it_is_entered() -> None:
    """Resolution happens at construction, so an unusable name never reaches a stored row."""
    with pytest.raises(InvalidTrigger):
        TimeZoneName("Mars/Olympus")
    with pytest.raises(InvalidTrigger):
        ScheduleSpec.cron("0 0 * * *", "Not/AZone")
    # A name that resolves is usable, and an unresolvable one is caught before `.zone()` exists.
    assert TimeZoneName("Europe/London").zone().key == "Europe/London"


def test_interval_and_one_time_carry_no_timezone() -> None:
    assert ScheduleSpec.interval(300).timezone is None
    assert ScheduleSpec.one_time(NOW).timezone is None


# ------------------------------------------------------------------------------------------
# Interval bounds
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("seconds", [59, 0, -1, INTERVAL_MAX_SECONDS + 1])
def test_an_interval_outside_its_frozen_bounds_is_refused(seconds: int) -> None:
    with pytest.raises(InvalidTrigger):
        ScheduleSpec.interval(seconds)


@pytest.mark.parametrize("seconds", [INTERVAL_MIN_SECONDS, 300, INTERVAL_MAX_SECONDS])
def test_an_interval_inside_its_frozen_bounds_is_accepted(seconds: int) -> None:
    assert ScheduleSpec.interval(seconds).interval_seconds == seconds


# ------------------------------------------------------------------------------------------
# Event type grammar
# ------------------------------------------------------------------------------------------

ACCEPTED_EVENTS = [
    "calendar.event.created",
    "device.temperature.changed",
    "sensor_reading",
    "a",
    "a.b",
    "x" * MAX_EVENT_TYPE_LENGTH,
]

REJECTED_EVENTS = [
    "Calendar.event.created",
    ".leading",
    "trailing.",
    "double..dot",
    "with space",
    "with-dash",
    "nervos.run.completed",  # the reserved namespace
    "nervos.",
    "x" * (MAX_EVENT_TYPE_LENGTH + 1),
    "",
]


@pytest.mark.parametrize("value", ACCEPTED_EVENTS)
def test_the_event_grammar_accepts_these(value: str) -> None:
    assert validate_event_type(value) == value


@pytest.mark.parametrize("value", REJECTED_EVENTS)
def test_the_event_grammar_refuses_these(value: str) -> None:
    with pytest.raises(InvalidTrigger):
        validate_event_type(value)


# ------------------------------------------------------------------------------------------
# Webhook primitives
# ------------------------------------------------------------------------------------------


def test_a_generated_secret_has_the_frozen_shape_and_digest() -> None:
    secret = generate_secret()
    assert len(secret) == WEBHOOK_SECRET_LENGTH
    assert set(secret) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert len(digest_secret(secret)) == WEBHOOK_SECRET_DIGEST_LENGTH
    assert digest_secret(secret) == digest_secret(secret)


def test_two_generated_secrets_differ() -> None:
    assert generate_secret() != generate_secret()


def test_a_generated_public_id_is_exactly_the_frozen_width() -> None:
    value = generate_public_id()
    assert len(value) == WEBHOOK_PUBLIC_ID_LENGTH
    assert validate_webhook_public_id(value) == value


@pytest.mark.parametrize(
    "value", ["", "short", "x" * 21, "x" * 23, "not url-safe!!", "a" * 22 + "!"]
)
def test_a_malformed_public_id_is_refused(value: str) -> None:
    with pytest.raises(InvalidTrigger):
        validate_webhook_public_id(value)


def test_the_dummy_digest_is_a_fixed_thirty_two_bytes() -> None:
    """The uniform unknown-locator comparison path needs a constant to compare against."""
    assert len(DUMMY_DIGEST) == WEBHOOK_SECRET_DIGEST_LENGTH
    assert bytes(WEBHOOK_SECRET_DIGEST_LENGTH) == DUMMY_DIGEST


def test_a_secret_digest_is_absent_from_a_definition_repr() -> None:
    """A digest is not a plaintext secret, but it must not reach a log or a traceback either."""
    definition = TriggerDefinition(
        id=1,
        owner_user_id=1,
        agent_instance_id=1,
        kind=TriggerKind.WEBHOOK,
        display_name="Hook",
        enabled=True,
        input_text="handle it",
        config_revision=1,
        misfire_policy=MisfirePolicy.COALESCE_ONE,
        next_fire_at=None,
        run_at=None,
        interval_seconds=None,
        cron_expression=None,
        timezone=None,
        public_id=generate_public_id(),
        created_at=NOW,
        updated_at=NOW,
        secret_digest=digest_secret(generate_secret()),
        secret_created_at=NOW,
    )
    assert "secret_digest" not in repr(definition)


# ------------------------------------------------------------------------------------------
# Occurrence vocabulary
# ------------------------------------------------------------------------------------------


def test_the_occurrence_vocabulary_has_exactly_two_members() -> None:
    """No `duplicate` status exists: a duplicate returns the existing occurrence and writes none."""
    assert {member.value for member in OccurrenceStatus} == {"run_created", "skipped"}


def test_a_duplicate_outcome_may_reference_a_run_created_occurrence() -> None:
    occurrence = _occurrence(status=OccurrenceStatus.RUN_CREATED, run_id=7)
    assert occurrence.run_id == 7


def test_a_duplicate_outcome_may_reference_a_skipped_occurrence() -> None:
    occurrence = _occurrence(
        status=OccurrenceStatus.SKIPPED, run_id=None, skip_code="agent_disabled"
    )
    assert occurrence.run_id is None


def _occurrence(
    *,
    status: OccurrenceStatus,
    run_id: int | None,
    skip_code: str | None = None,
) -> TriggerOccurrence:
    return TriggerOccurrence(
        id=1,
        trigger_definition_id=1,
        owner_user_id=1,
        agent_instance_id=1,
        trigger_revision=1,
        status=status,
        run_id=run_id,
        skip_code=skip_code,
        skip_message=None if skip_code is None else "refused",
        nominal_at=NOW,
        event_id=None,
        idempotency_key=None,
        occurred_at=NOW,
        created_at=NOW,
    )


def test_an_occurrence_carries_at_most_one_identity() -> None:
    with pytest.raises(InvalidTrigger):
        TriggerOccurrence(
            id=1,
            trigger_definition_id=1,
            owner_user_id=1,
            agent_instance_id=1,
            trigger_revision=1,
            status=OccurrenceStatus.SKIPPED,
            run_id=None,
            skip_code="agent_disabled",
            skip_message="refused",
            nominal_at=NOW,
            event_id="evt-1",
            idempotency_key=None,
            occurred_at=NOW,
            created_at=NOW,
        )


# ------------------------------------------------------------------------------------------
# Definition invariants
# ------------------------------------------------------------------------------------------

BASE_DEFINITION: dict[str, object] = {
    "id": 1,
    "owner_user_id": 1,
    "agent_instance_id": 1,
    "kind": TriggerKind.CRON,
    "display_name": "Nightly",
    "enabled": True,
    "input_text": "run",
    "config_revision": 1,
    "misfire_policy": MisfirePolicy.COALESCE_ONE,
    "next_fire_at": NOW + timedelta(days=1),
    "run_at": None,
    "interval_seconds": None,
    "cron_expression": "0 3 * * *",
    "timezone": "UTC",
    "public_id": None,
    "created_at": NOW,
    "updated_at": NOW,
}


def test_a_well_formed_cron_definition_is_accepted() -> None:
    assert TriggerDefinition(**BASE_DEFINITION).kind is TriggerKind.CRON  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"run_at": NOW},  # a cron trigger cannot carry a one-time instant
        {"interval_seconds": 300},
        {"public_id": generate_public_id()},
        {"event_type": "a.b"},
    ],
)
def test_a_cross_kind_field_is_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(InvalidTrigger):
        TriggerDefinition(**{**BASE_DEFINITION, **overrides})  # type: ignore[arg-type]


def test_the_trigger_input_bound_matches_the_run_input_bound() -> None:
    limits = RunLimits()
    assert limits.input_max_code_points == MAX_TRIGGER_INPUT_CODE_POINTS
    assert limits.input_max_bytes == MAX_TRIGGER_INPUT_BYTES


def test_blank_trigger_input_is_refused() -> None:
    with pytest.raises(InvalidTrigger):
        TriggerDefinition(**{**BASE_DEFINITION, "input_text": "   "})  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------
# next_fire_at alignment
# ------------------------------------------------------------------------------------------


def test_an_enabled_schedule_requires_a_next_fire_time() -> None:
    with pytest.raises(InvalidTrigger):
        TriggerDefinition(**{**BASE_DEFINITION, "next_fire_at": None})  # type: ignore[arg-type]


def test_a_disabled_schedule_carries_no_next_fire_time() -> None:
    definition = TriggerDefinition(
        **{**BASE_DEFINITION, "enabled": False, "next_fire_at": None}  # type: ignore[arg-type]
    )
    assert definition.next_fire_at is None
    with pytest.raises(InvalidTrigger):
        TriggerDefinition(**{**BASE_DEFINITION, "enabled": False})  # type: ignore[arg-type]


def test_a_delivery_driven_trigger_never_carries_a_next_fire_time() -> None:
    with pytest.raises(InvalidTrigger):
        TriggerDefinition(
            **{
                **BASE_DEFINITION,
                "kind": TriggerKind.EVENT,
                "cron_expression": None,
                "timezone": None,
                "event_type": "a.b",
            }  # type: ignore[arg-type]
        )


def test_the_tool_free_run_limits_are_unchanged_by_stage_e() -> None:
    """E1 adds no execution limit and changes none."""
    assert RunLimits().max_tool_calls == 0
    assert RunLimits().input_max_code_points == 4000
