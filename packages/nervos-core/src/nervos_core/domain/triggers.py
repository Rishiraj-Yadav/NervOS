"""Stage E trigger, occurrence, and schedule domain values.

This module is provider-neutral and dependency-free. It holds no database type, no HTTP type, and
no cron-evaluation library: it decides what a trigger *is*, what an occurrence *records*, and
whether a proposed configuration is representable at all.

Two divisions shape everything below, and both are frozen by ADR 0018:

* A **TriggerDefinition** is current configuration — the only thing a user edits.
* A **TriggerOccurrence** is the immutable, durable fact that a trigger became due or that
  something was delivered to it. It is written once, with its final status, and never updated.

Neither is execution. A Run created from an occurrence is an ordinary Run, and every question about
what happened during it is answered by Run/Job/Attempt, never by these values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.runs import InvalidRun, RunLimits, validate_input_text

# ------------------------------------------------------------------------------------------------
# Bounds. Module-level constants rather than operator settings, following the repository's rule that
# an exposed setting needs a demonstrated operator requirement.
# ------------------------------------------------------------------------------------------------

INTERVAL_MIN_SECONDS = 60
# One year. The bound exists so interval arithmetic cannot overflow a timedelta and so absurd
# integer input is refused where it is entered; long calendar scheduling belongs to cron or a
# one-time trigger, not to a 365-day interval.
INTERVAL_MAX_SECONDS = 31_536_000

MAX_CRON_EXPRESSION_LENGTH = 128
MAX_TIMEZONE_NAME_LENGTH = 64
MAX_EVENT_TYPE_LENGTH = 64
MAX_EVENT_ID_LENGTH = 128
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MAX_DISPLAY_NAME_CHARS = 400
MAX_DISPLAY_NAME_BYTES = 1600
MAX_SKIP_MESSAGE_LENGTH = 512

# A webhook locator is `secrets.token_urlsafe(16)`: 16 bytes of entropy in exactly 22 URL-safe
# characters. The exact width is frozen here so the domain and the database agree on it, rather than
# the database accepting any 1..64 character string and calling that a shape.
WEBHOOK_PUBLIC_ID_LENGTH = 22
WEBHOOK_PUBLIC_ID_BYTES = 16
# A webhook secret is `secrets.token_urlsafe(32)`: 256 bits in exactly 43 URL-safe characters.
WEBHOOK_SECRET_LENGTH = 43
WEBHOOK_SECRET_BYTES = 32
# A machine-token digest is a bare SHA-256: 32 bytes, never a KDF output.
WEBHOOK_SECRET_DIGEST_LENGTH = 32

# The trigger's static input is bounded by the same maximum a Run's own input is, so a stored
# trigger can never carry text the Run it creates would refuse.
MAX_TRIGGER_INPUT_CODE_POINTS = RunLimits().input_max_code_points
MAX_TRIGGER_INPUT_BYTES = RunLimits().input_max_bytes

_UNSAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SAFE_TEXT_FORBIDDEN = "\x00"
# The URL-safe base64 alphabet `secrets.token_urlsafe` produces.
_PUBLIC_ID_PATTERN = re.compile(rf"[A-Za-z0-9_-]{{{WEBHOOK_PUBLIC_ID_LENGTH}}}\Z")
_SECRET_PATTERN = re.compile(rf"[A-Za-z0-9_-]{{{WEBHOOK_SECRET_LENGTH}}}\Z")

# Event types are lowercase dotted names: `calendar.event.created`. Each segment begins with a
# letter; `nervos.*` is reserved for future system events and is rejected from user publication.
_EVENT_TYPE_PATTERN = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\Z")
RESERVED_EVENT_TYPE_PREFIX = "nervos."


class InvalidTrigger(ValueError):
    """A Stage E trigger value violates the frozen ADR 0018 contract."""


# ------------------------------------------------------------------------------------------------
# Vocabulary
# ------------------------------------------------------------------------------------------------


class TriggerKind(StrEnum):
    """The five user-facing sources of an occurrence. No further kind is added in Stage E."""

    ONE_TIME = "one_time"
    INTERVAL = "interval"
    CRON = "cron"
    WEBHOOK = "webhook"
    EVENT = "event"


# The three kinds that carry a next fire time. Webhook and event triggers are driven by delivery,
# never by a clock, so `next_fire_at` is always NULL for them.
SCHEDULE_KINDS: tuple[TriggerKind, ...] = (
    TriggerKind.ONE_TIME,
    TriggerKind.INTERVAL,
    TriggerKind.CRON,
)


class MisfirePolicy(StrEnum):
    """How missed nominal instants are reconciled on resume.

    Exactly one member in Stage E, stored as a column rather than a boolean so a second policy is
    later a migration that widens one `IN` list — no data migration and no reshape.
    """

    #: Materialize at most one catch-up occurrence at the latest missed nominal instant, then
    #: advance to the first strictly-future one. Unbounded historical backfill is refused.
    COALESCE_ONE = "coalesce_one"


class OccurrenceStatus(StrEnum):
    """The complete occurrence vocabulary: two terminal statuses, and nothing else.

    There is deliberately no `duplicate` member. A duplicate delivery or publication refers to an
    occurrence that already exists, so it returns the existing row and writes nothing — the
    duplicate fact is ephemeral, carried in the caller's result, never persisted. There is also no
    `pending` member: occurrence insertion and Run submission commit in one transaction, so no
    occurrence can exist without its outcome and no second worker is needed to sweep one.
    """

    RUN_CREATED = "run_created"
    SKIPPED = "skipped"


class SkipReason(StrEnum):
    """Why a recognised occurrence produced no Run. Static, safe, and never interpolated."""

    AGENT_DISABLED = "agent_disabled"
    AGENT_UNAVAILABLE = "agent_unavailable"
    INPUT_TOO_LARGE = "input_too_large"
    SCHEDULE_INVALID = "schedule_invalid"
    SCHEDULE_TIMEZONE_INVALID = "schedule_timezone_invalid"
    TRIGGER_SUBMISSION_FAILED = "trigger_submission_failed"


#: The static operator-facing text for each skip reason. Never composed from payload or config.
SKIP_MESSAGES: dict[SkipReason, str] = {
    SkipReason.AGENT_DISABLED: "The agent instance is not eligible to create new runs.",
    SkipReason.AGENT_UNAVAILABLE: "The agent instance is not eligible to create new runs.",
    SkipReason.INPUT_TOO_LARGE: "The automation's input is too large for this agent.",
    SkipReason.SCHEDULE_INVALID: "This automation's schedule could not be evaluated.",
    SkipReason.SCHEDULE_TIMEZONE_INVALID: "This automation's timezone is no longer available.",
    SkipReason.TRIGGER_SUBMISSION_FAILED: "The automation could not create a run.",
}


class RunOrigin(StrEnum):
    """Why a Run exists. **Derived**, never persisted.

    A manual Run has no occurrence pointing at it; a triggered Run has exactly one. Origin is
    therefore read from that relation rather than stored, so it can never drift from the record
    that actually explains it.
    """

    MANUAL = "manual"
    ONE_TIME = "one_time"
    INTERVAL = "interval"
    CRON = "cron"
    WEBHOOK = "webhook"
    EVENT = "event"


#: The origin a Run created by each trigger kind reports.
ORIGIN_BY_KIND: dict[TriggerKind, RunOrigin] = {
    TriggerKind.ONE_TIME: RunOrigin.ONE_TIME,
    TriggerKind.INTERVAL: RunOrigin.INTERVAL,
    TriggerKind.CRON: RunOrigin.CRON,
    TriggerKind.WEBHOOK: RunOrigin.WEBHOOK,
    TriggerKind.EVENT: RunOrigin.EVENT,
}


# Which of the eight kind-specific fields each kind must set. Every other one of the eight must be
# None, which is what makes an illegal trigger unrepresentable. `next_fire_at` is deliberately
# absent: the alignment rule below owns it.
_KIND_SHAPE_FIELDS = frozenset(
    {
        "run_at",
        "interval_seconds",
        "cron_expression",
        "timezone",
        "public_id",
        "secret_digest",
        "secret_created_at",
        "event_type",
    }
)
_KIND_REQUIRED_FIELDS: dict[TriggerKind, frozenset[str]] = {
    TriggerKind.ONE_TIME: frozenset({"run_at"}),
    TriggerKind.INTERVAL: frozenset({"interval_seconds"}),
    TriggerKind.CRON: frozenset({"cron_expression", "timezone"}),
    TriggerKind.WEBHOOK: frozenset({"public_id", "secret_digest", "secret_created_at"}),
    TriggerKind.EVENT: frozenset({"event_type"}),
}


def _validate_trigger_input(value: str) -> None:
    """Validate input text with the Run's own bounds, reporting a trigger-domain failure.

    The bounds are deliberately borrowed from `RunLimits` rather than restated, so a stored trigger
    can never carry text the Run it creates would refuse. The exception is translated so a caller
    of the trigger domain sees one error type rather than two.
    """
    try:
        validate_input_text(value, RunLimits())
    except InvalidRun as error:
        raise InvalidTrigger("invalid trigger input text") from error


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidTrigger("timestamp must be timezone-aware")
    return value.astimezone(UTC)


# ------------------------------------------------------------------------------------------------
# Value objects
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimeZoneName:
    """A resolved IANA timezone name.

    Resolution happens at creation time, so an unusable name is refused where it is entered rather
    than discovered when a trigger fires. `zoneinfo` is the standard library and reads a read-only
    data source, exactly as this package's existing use of `unicodedata` does; it is not an
    infrastructure dependency and keeps the check where every other domain check lives.
    """

    name: str

    def __post_init__(self) -> None:
        value = self.name
        if (
            not value
            or len(value) > MAX_TIMEZONE_NAME_LENGTH
            or _SAFE_TEXT_FORBIDDEN in value
            or value.strip() != value
        ):
            raise InvalidTrigger("invalid timezone name")
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise InvalidTrigger("unknown timezone name") from error

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.name)

    def __str__(self) -> str:
        return self.name


#: The timezone a cron trigger carries when the user does not name one. It is stored, not left
#: NULL, so the column is total for cron and no later reader needs a null branch.
DEFAULT_TIMEZONE = "UTC"

_MONTH_NAMES = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_WEEKDAY_NAMES = {
    "SUN": 0,
    "MON": 1,
    "TUE": 2,
    "WED": 3,
    "THU": 4,
    "FRI": 5,
    "SAT": 6,
}
# (minimum, maximum, names) per accepted field, in field order. Day-of-week accepts 0..7 so the
# conventional 0-and-7-as-Sunday spelling stays legal.
_CRON_FIELDS: tuple[tuple[int, int, dict[str, int] | None], ...] = (
    (0, 59, None),  # minute
    (0, 23, None),  # hour
    (1, 31, None),  # day of month
    (1, 12, _MONTH_NAMES),  # month
    (0, 7, _WEEKDAY_NAMES),  # day of week
)
_CRON_FIELD_COUNT = len(_CRON_FIELDS)
_CRON_ITEM = re.compile(
    r"\*|(\d+)(?:-(\d+))?(?:/(\d+))?|([A-Za-z]{3})(?:-([A-Za-z]{3}))?(?:/(\d+))?\Z"
)


def _cron_item_is_valid(item: str, low: int, high: int, names: dict[str, int] | None) -> bool:
    """One comma-separated item of one cron field.

    The accepted shape is deliberately narrow: `*`, `*/n`, `v`, `v-v`, `v-v/n`, and the same with
    three-letter names where the field allows them. A bare `v/n` is **refused**, as are the `L`,
    `LW`, `#` and `?` extensions and seconds — the frozen NervOS dialect is a strict subset of what
    any evaluation library accepts, so adopting a library later cannot widen it.
    """
    if item == "*":
        return True
    if item.startswith("*/"):
        step_token = item[2:]
        if not step_token.isdigit():
            return False
        return 1 <= int(step_token) <= high
    match = _CRON_ITEM.fullmatch(item)
    if match is None:
        return False
    numeric_from, numeric_to, numeric_step, name_from, name_to, name_step = match.groups()

    if names is not None and (name_from is not None or name_to is not None):
        if numeric_from is not None or numeric_to is not None or name_step is not None:
            return False
        step_token = name_step
        start = names.get(name_from.upper()) if name_from else None
        end = names.get(name_to.upper()) if name_to else None
    else:
        step_token = numeric_step
        start = int(numeric_from) if numeric_from is not None else None
        end = int(numeric_to) if numeric_to is not None else None

    if start is None or not low <= start <= high:
        return False
    if end is not None and not low <= end <= high:
        return False
    if end is not None and end < start:
        return False
    if step_token is not None:
        step = int(step_token)
        if not 1 <= step <= high:
            return False
    return True


@dataclass(frozen=True, slots=True)
class CronExpression:
    """A validated five-field cron expression in the frozen NervOS dialect.

    `text` is the user's exact string and is **never rewritten**: a normalized expression would make
    `trigger_revision` untrustworthy. Only syntax is checked here — computing the next instant is
    E2's job and is deliberately absent, which is what lets E1 ship with no cron dependency.
    """

    text: str

    def __post_init__(self) -> None:
        value = self.text
        if (
            not value
            or len(value) > MAX_CRON_EXPRESSION_LENGTH
            or _SAFE_TEXT_FORBIDDEN in value
            or value.strip() != value
        ):
            raise InvalidTrigger("invalid cron expression")
        fields = value.split()
        if len(fields) != _CRON_FIELD_COUNT:
            # Five fields exactly. A sixth seconds field is refused rather than ignored: sub-minute
            # scheduling is the largest amplification risk available and nothing requires it.
            raise InvalidTrigger("cron expression must have exactly five fields")
        for field_text, (low, high, names) in zip(fields, _CRON_FIELDS, strict=True):
            for item in field_text.split(","):
                if not item or not _cron_item_is_valid(item, low, high, names):
                    raise InvalidTrigger("invalid cron field")

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    """The schedule-defining configuration of one trigger, validated as a whole.

    Constructed only through the three factories, so a kind can never carry another kind's fields.
    """

    kind: TriggerKind
    run_at: datetime | None = None
    interval_seconds: int | None = None
    cron_expression: CronExpression | None = None
    timezone: TimeZoneName | None = None

    def __post_init__(self) -> None:
        if self.kind is TriggerKind.ONE_TIME:
            if (
                self.run_at is None
                or self.interval_seconds is not None
                or self.cron_expression is not None
            ):
                raise InvalidTrigger("one_time requires run_at and no other schedule field")
            if self.timezone is not None:
                raise InvalidTrigger("one_time carries no timezone")
            object.__setattr__(self, "run_at", _utc(self.run_at))
        elif self.kind is TriggerKind.INTERVAL:
            if (
                self.interval_seconds is None
                or self.run_at is not None
                or self.cron_expression is not None
            ):
                raise InvalidTrigger(
                    "interval requires interval_seconds and no other schedule field"
                )
            if not INTERVAL_MIN_SECONDS <= self.interval_seconds <= INTERVAL_MAX_SECONDS:
                raise InvalidTrigger("interval is outside its frozen bounds")
            if self.timezone is not None:
                # Interval is fixed-duration UTC-second scheduling. A user who means "09:00 local
                # every day" uses cron, not a 24-hour interval.
                raise InvalidTrigger("interval carries no timezone")
        elif self.kind is TriggerKind.CRON:
            if (
                self.cron_expression is None
                or self.run_at is not None
                or self.interval_seconds is not None
            ):
                raise InvalidTrigger("cron requires a cron expression and no other schedule field")
            if self.timezone is None:
                object.__setattr__(self, "timezone", TimeZoneName(DEFAULT_TIMEZONE))
        else:
            raise InvalidTrigger("a schedule spec is only for a schedule kind")

    @staticmethod
    def one_time(run_at: datetime) -> ScheduleSpec:
        return ScheduleSpec(kind=TriggerKind.ONE_TIME, run_at=run_at)

    @staticmethod
    def interval(interval_seconds: int) -> ScheduleSpec:
        return ScheduleSpec(kind=TriggerKind.INTERVAL, interval_seconds=interval_seconds)

    @staticmethod
    def cron(cron_expression: str, timezone: str = DEFAULT_TIMEZONE) -> ScheduleSpec:
        return ScheduleSpec(
            kind=TriggerKind.CRON,
            cron_expression=CronExpression(cron_expression),
            timezone=TimeZoneName(timezone),
        )


def validate_event_type(value: str) -> str:
    """Validate an internal event type in the frozen dotted-name grammar.

    This function is the authority. The database carries the strongest subset SQLite can express
    (leading letter, accepted character set, length, no NUL); the remaining clauses — no leading or
    trailing dot, no consecutive dots, no reserved-namespace publication — are enforced here, and
    the persistence tests say so rather than claiming the database enforces all of it.
    """
    if (
        not value
        or len(value) > MAX_EVENT_TYPE_LENGTH
        or _SAFE_TEXT_FORBIDDEN in value
        or _EVENT_TYPE_PATTERN.fullmatch(value) is None
    ):
        raise InvalidTrigger("invalid event type")
    if value.startswith(RESERVED_EVENT_TYPE_PREFIX):
        raise InvalidTrigger("the nervos.* event namespace is reserved")
    return value


def validate_webhook_public_id(value: str) -> str:
    """Validate a webhook locator. It is unguessable, and it authenticates nothing."""
    if _PUBLIC_ID_PATTERN.fullmatch(value) is None:
        raise InvalidTrigger("invalid webhook public id")
    return value


def validate_webhook_secret_digest(value: bytes) -> bytes:
    if len(value) != WEBHOOK_SECRET_DIGEST_LENGTH:
        raise InvalidTrigger("invalid webhook secret digest")
    return value


def validate_skip(value: str, message: str) -> tuple[str, str]:
    """Validate a skip code and its static message, in the shape the Job domain already uses."""
    if _UNSAFE_CODE.fullmatch(value) is None:
        raise InvalidTrigger("invalid skip code")
    if (
        not message.strip()
        or len(message) > MAX_SKIP_MESSAGE_LENGTH
        or _SAFE_TEXT_FORBIDDEN in message
    ):
        raise InvalidTrigger("invalid skip message")
    return value, message


def _validate_display_name(value: str) -> str:
    if (
        not value.strip()
        or len(value) > MAX_DISPLAY_NAME_CHARS
        or len(value.encode("utf-8")) > MAX_DISPLAY_NAME_BYTES
        or _SAFE_TEXT_FORBIDDEN in value
    ):
        raise InvalidTrigger("invalid display name")
    return value


# ------------------------------------------------------------------------------------------------
# Durable facts
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TriggerDefinition:
    """Current operator configuration for one trigger. Configuration authority."""

    id: int
    owner_user_id: int
    agent_instance_id: int
    kind: TriggerKind
    display_name: str
    enabled: bool
    input_text: str
    config_revision: int
    misfire_policy: MisfirePolicy
    next_fire_at: datetime | None
    run_at: datetime | None
    interval_seconds: int | None
    cron_expression: str | None
    timezone: str | None
    public_id: str | None
    created_at: datetime
    updated_at: datetime
    # A SHA-256 digest is not a plaintext secret, but credential material must not be printed into a
    # log or a traceback by a dataclass repr either.
    secret_digest: bytes | None = field(default=None, repr=False)
    secret_created_at: datetime | None = None
    event_type: str | None = None
    # Derived, read-only projection of the joined `agent_instances` row, supplied only by the
    # owner-scoped read paths that join it. It is never persisted on the trigger and carries no
    # authority: materialization needs the target Agent's exact definition identity in order to
    # resolve its limits the same way a manual submission does. A trigger read straight from its
    # own row therefore leaves it None.
    agent_definition_id: AgentDefinitionId | None = None

    def __post_init__(self) -> None:
        # `id == 0` is a value that has no durable identity yet: persistence builds the domain value
        # first so it validates before the write, then reads it back with the assigned id.
        if self.id < 0 or self.owner_user_id <= 0 or self.agent_instance_id <= 0:
            raise InvalidTrigger
        if self.config_revision <= 0:
            raise InvalidTrigger("invalid config revision")
        if type(self.enabled) is not bool:
            raise InvalidTrigger("enabled must be a boolean")
        _validate_display_name(self.display_name)
        _validate_trigger_input(self.input_text)
        self._require_kind_shape()
        self._require_next_fire_alignment()
        created = _utc(self.created_at)
        updated = _utc(self.updated_at)
        if updated < created:
            raise InvalidTrigger("invalid timestamp order")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)
        if self.next_fire_at is not None:
            object.__setattr__(self, "next_fire_at", _utc(self.next_fire_at))
        if self.run_at is not None:
            object.__setattr__(self, "run_at", _utc(self.run_at))
        if self.secret_created_at is not None:
            object.__setattr__(self, "secret_created_at", _utc(self.secret_created_at))

    def _require_kind_shape(self) -> None:
        """Exactly the fields this kind permits are set, and every other kind field is None.

        Mirrors the database's exhaustive five-branch `kind_shape` CHECK. Both state the same rule
        so an illegal trigger is refused at construction *and* unrepresentable in the table.
        `next_fire_at` is deliberately absent here: it is `_require_next_fire_alignment`'s subject,
        and checking it in both places would give one rule two owners.
        """
        values: dict[str, object] = {
            "run_at": self.run_at,
            "interval_seconds": self.interval_seconds,
            "cron_expression": self.cron_expression,
            "timezone": self.timezone,
            "public_id": self.public_id,
            "secret_digest": self.secret_digest,
            "secret_created_at": self.secret_created_at,
            "event_type": self.event_type,
        }
        if set(values) != set(_KIND_SHAPE_FIELDS):
            raise InvalidTrigger("trigger kind shape fields drifted from their frozen set")
        required = _KIND_REQUIRED_FIELDS[self.kind]
        for name, value in values.items():
            if name in required:
                if value is None:
                    raise InvalidTrigger(f"a {self.kind} trigger requires {name}")
            elif value is not None:
                raise InvalidTrigger(f"a {self.kind} trigger carries no {name}")

        if self.kind is TriggerKind.INTERVAL:
            seconds = self.interval_seconds
            if seconds is None or not INTERVAL_MIN_SECONDS <= seconds <= INTERVAL_MAX_SECONDS:
                raise InvalidTrigger("invalid interval bounds")
        elif self.kind is TriggerKind.CRON:
            CronExpression(self.cron_expression or "")
            TimeZoneName(self.timezone or "")
        elif self.kind is TriggerKind.WEBHOOK:
            validate_webhook_public_id(self.public_id or "")
            validate_webhook_secret_digest(self.secret_digest or b"")
        elif self.kind is TriggerKind.EVENT:
            validate_event_type(self.event_type or "")

    def _require_next_fire_alignment(self) -> None:
        """An enabled schedule always has a next fire time; a delivery-driven kind never has one.

        Both terminal paths converge on "disabled" — a one-time trigger whose sole occurrence has
        been materialized, and a cron trigger whose stored timezone became unresolvable — so the
        rule has no exception and a stale value is a construction error rather than a state a later
        reader has to reason about.
        """
        if self.kind in SCHEDULE_KINDS:
            if self.enabled and self.next_fire_at is None:
                raise InvalidTrigger("an enabled schedule requires a next fire time")
            if not self.enabled and self.next_fire_at is not None:
                raise InvalidTrigger("a disabled schedule carries no next fire time")
        elif self.next_fire_at is not None:
            raise InvalidTrigger("a delivery-driven trigger carries no next fire time")

    @property
    def is_schedule(self) -> bool:
        return self.kind in SCHEDULE_KINDS


@dataclass(frozen=True, slots=True)
class TriggerOccurrence:
    """The immutable, durable fact that a trigger became due or was delivered to.

    Written once with its final status; there is no update path and no partial state. The row
    answers *did this trigger create this Run?* and never *what happened during that Run*.

    There is deliberately no `kind` field: `TriggerDefinition.kind` is immutable, the reference is
    RESTRICT, and a definition cannot be deleted while history exists, so the kind is always
    derivable and a stored copy could only be a second version of one fact.
    """

    id: int
    trigger_definition_id: int
    owner_user_id: int
    agent_instance_id: int
    trigger_revision: int
    status: OccurrenceStatus
    run_id: int | None
    skip_code: str | None
    skip_message: str | None
    nominal_at: datetime | None
    event_id: str | None
    idempotency_key: str | None
    occurred_at: datetime
    created_at: datetime
    payload_digest: bytes | None = field(default=None, repr=False)
    payload_bytes: int | None = None

    def __post_init__(self) -> None:
        # `id == 0` is a value that has no durable identity yet, exactly as for TriggerDefinition.
        if (
            self.id < 0
            or self.trigger_definition_id <= 0
            or self.owner_user_id <= 0
            or self.agent_instance_id <= 0
        ):
            raise InvalidTrigger
        if self.trigger_revision <= 0:
            raise InvalidTrigger("invalid trigger revision")
        # The complete two-status shape. There is no third branch, so a duplicate cannot be
        # constructed as a status at all.
        if self.status is OccurrenceStatus.RUN_CREATED:
            if self.run_id is None or self.skip_code is not None or self.skip_message is not None:
                raise InvalidTrigger("a materialized occurrence carries a run and no skip")
        elif self.run_id is not None or self.skip_code is None or self.skip_message is None:
            raise InvalidTrigger("a skipped occurrence carries a skip and no run")
        else:
            validate_skip(self.skip_code, self.skip_message)
        self._require_identity_shape()
        if self.payload_digest is None and self.payload_bytes is not None:
            raise InvalidTrigger("a payload byte count requires a digest")
        if self.payload_digest is not None:
            validate_webhook_secret_digest(self.payload_digest)
            if self.payload_bytes is None or self.payload_bytes < 0:
                raise InvalidTrigger("invalid payload size")
        if self.event_id is not None and (
            not self.event_id
            or len(self.event_id) > MAX_EVENT_ID_LENGTH
            or _SAFE_TEXT_FORBIDDEN in self.event_id
        ):
            raise InvalidTrigger("invalid event id")
        if self.idempotency_key is not None and (
            not self.idempotency_key
            or len(self.idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH
            or _SAFE_TEXT_FORBIDDEN in self.idempotency_key
        ):
            raise InvalidTrigger("invalid idempotency key")
        occurred = _utc(self.occurred_at)
        created = _utc(self.created_at)
        if created < occurred:
            raise InvalidTrigger("invalid timestamp order")
        object.__setattr__(self, "occurred_at", occurred)
        object.__setattr__(self, "created_at", created)
        if self.nominal_at is not None:
            object.__setattr__(self, "nominal_at", _utc(self.nominal_at))

    def _require_identity_shape(self) -> None:
        """At most one dedupe identity per row.

        A keyless webhook delivery has **no** deterministic identity by design, so all three fields
        being absent is legal and means "never deduped". Every other combination is refused, and the
        database enforces the same set. Without this, an occurrence could carry a schedule identity
        whose instant is NULL — and because SQLite treats NULLs as distinct under a UNIQUE index,
        dedupe would silently stop working for exactly those rows.
        """
        present = [
            self.nominal_at is not None,
            self.event_id is not None,
            self.idempotency_key is not None,
        ]
        if sum(present) > 1:
            raise InvalidTrigger("an occurrence carries at most one dedupe identity")
