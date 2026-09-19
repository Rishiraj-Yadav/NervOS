"""Owner-scoped TriggerDefinition and TriggerOccurrence persistence.

Every mutation runs through the shared `TransactionRunner`, so a trigger write inherits the same
one-`BEGIN IMMEDIATE`-per-operation policy every other durable mutation in NervOS uses.

The load-bearing method here is `materialize_occurrence_and_run`. It is the only place in the
repository that creates a Run without a person having pressed a button, and it does so by calling
the *same* `insert_run_and_job_on_connection` helper a manual submission calls — inside one
transaction, on the caller's connection — so the occurrence, the Run, the Job and the two opening
events either all commit or nothing happened.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import Engine, and_, delete, func, insert, or_, select, update
from sqlalchemy.engine import Connection, RowMapping

from nervos_core.application.agents import DurableSubmissionRejected
from nervos_core.application.errors import PersistenceUnavailable
from nervos_core.application.triggers import (
    DueScheduleCandidate,
    ScheduleMaterializationCommand,
    ScheduleMaterializationOutcome,
    ScheduleOutcomeKind,
    StaleReason,
    TriggerDraft,
    TriggerEdit,
    TriggerHasHistory,
    TriggerMaterializationCommand,
    TriggerMaterializationOutcome,
    TriggerNotEditable,
    TriggerNotFound,
)
from nervos_core.domain.agents import AgentDefinitionId
from nervos_core.domain.scheduling import schedule_of
from nervos_core.domain.triggers import (
    SCHEDULE_KINDS,
    SKIP_MESSAGES,
    InvalidTrigger,
    MisfirePolicy,
    OccurrenceStatus,
    ScheduleSpec,
    SkipReason,
    TriggerDefinition,
    TriggerKind,
    TriggerOccurrence,
)
from nervos_core.infrastructure.database.jobs import (
    insert_run_and_job_on_connection,
    validate_pending_cap,
)
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    TriggerDefinitionRecord,
    TriggerOccurrenceRecord,
)
from nervos_core.infrastructure.database.transaction import TransactionRunner

#: The fields whose change makes an edit *defining*, and therefore increments `config_revision`.
#: A rename is not one of them: the revision means "the configuration that determines what fires",
#: not "the row changed". Enablement is not one either — it has its own operation and changes when
#: a trigger runs, not what it means.
_DEFINING_FIELDS = (
    "input_text",
    "run_at",
    "interval_seconds",
    "cron_expression",
    "timezone",
    "event_type",
)


def _as_utc(value: object) -> datetime:
    """Normalize a stored instant so two reads of the same fact compare equal.

    SQLite hands back naive datetimes for a `DateTime(timezone=True)` column, so comparing a stored
    value against a command's aware instant would be comparing the right instant in the wrong
    shape. Normalizing both sides here is what makes the stale check exact rather than
    accidentally always-unequal.
    """
    if not isinstance(value, datetime):
        raise InvalidTrigger("a stored schedule instant was not a timestamp")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class SqlAlchemyTriggerPersistence:
    """Durable trigger configuration, occurrence history, and the atomic materialization seam."""

    #: Matches the durable submission primitive's own default, so a triggered Run and a manual Run
    #: carry the same claim budget.
    max_attempts = 3

    def __init__(
        self,
        engine: Engine,
        *,
        max_pending: int = 1000,
        max_pending_per_agent: int | None = None,
        max_pending_per_provider: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._engine = engine
        self._capacity = validate_pending_cap(max_pending, "max_pending")
        self._agent_capacity = validate_pending_cap(
            self._capacity if max_pending_per_agent is None else max_pending_per_agent,
            "max_pending_per_agent",
        )
        self._provider_capacity = validate_pending_cap(
            self._capacity if max_pending_per_provider is None else max_pending_per_provider,
            "max_pending_per_provider",
        )
        self._runner = TransactionRunner(engine, sleep)

    # ------------------------------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------------------------------

    def create_trigger(
        self, owner_user_id: int, draft: TriggerDraft, now: datetime
    ) -> TriggerDefinition:
        """Create one trigger targeting an Agent Instance the caller owns."""

        def operation(connection: Connection) -> TriggerDefinition:
            self._require_owned_instance(connection, owner_user_id, draft.agent_instance_id)
            candidate = self._candidate(
                id=0,
                owner=owner_user_id,
                agent_instance_id=draft.agent_instance_id,
                kind=draft.kind,
                display_name=draft.display_name,
                enabled=draft.enabled,
                input_text=draft.input_text,
                config_revision=1,
                misfire_policy=draft.misfire_policy,
                schedule=draft.schedule,
                next_fire_at=draft.next_fire_at,
                public_id=draft.public_id,
                secret_digest=draft.secret_digest,
                secret_created_at=draft.secret_created_at,
                event_type=draft.event_type,
                created_at=now,
                updated_at=now,
            )
            result = connection.execute(
                insert(TriggerDefinitionRecord).values(**self._column_values(candidate))
            )
            primary_key = result.inserted_primary_key
            if primary_key is None or primary_key[0] is None:
                raise PersistenceUnavailable
            return self._read_definition(connection, owner_user_id, int(primary_key[0]))

        return self._runner.run(operation)

    def get_trigger(self, owner_user_id: int, trigger_id: int) -> TriggerDefinition:
        with self._engine.connect() as connection:
            return self._read_definition(connection, owner_user_id, trigger_id)

    def list_triggers(
        self, owner_user_id: int, limit: int, before_id: int | None
    ) -> tuple[TriggerDefinition, ...]:
        query = select(TriggerDefinitionRecord).where(
            TriggerDefinitionRecord.owner_user_id == owner_user_id
        )
        if before_id is not None:
            query = query.where(TriggerDefinitionRecord.id < before_id)
        query = query.order_by(TriggerDefinitionRecord.id.desc()).limit(limit)
        with self._engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        return tuple(self._to_definition(row) for row in rows)

    def update_trigger(
        self, owner_user_id: int, trigger_id: int, edit: TriggerEdit, now: datetime
    ) -> TriggerDefinition:
        """Apply an edit, incrementing `config_revision` only when the change is defining.

        A schedule edit must carry the next fire time, because E1 does not compute one: deriving an
        instant from a cron expression is the schedule evaluator's job and it arrives with E2.
        """

        def operation(connection: Connection) -> TriggerDefinition:
            current = self._read_definition(connection, owner_user_id, trigger_id)
            if current.kind is TriggerKind.ONE_TIME and current.next_fire_at is None:
                raise TriggerNotEditable("a completed one-time trigger cannot be edited")
            if edit.schedule is not None and edit.next_fire_at is None:
                raise TriggerNotEditable("a schedule edit must carry the next fire time")
            # An edit that does not restate the schedule keeps the one the trigger already has: a
            # rename must not silently erase a cron expression and break the row's kind shape.
            schedule = edit.schedule if edit.schedule is not None else self._schedule_of(current)
            candidate = self._candidate(
                id=current.id,
                owner=current.owner_user_id,
                agent_instance_id=current.agent_instance_id,
                kind=current.kind,
                display_name=edit.display_name,
                enabled=current.enabled,
                input_text=edit.input_text,
                config_revision=current.config_revision,
                misfire_policy=current.misfire_policy,
                schedule=schedule,
                next_fire_at=(
                    edit.next_fire_at if edit.next_fire_at is not None else current.next_fire_at
                ),
                public_id=current.public_id,
                secret_digest=current.secret_digest,
                secret_created_at=current.secret_created_at,
                event_type=(
                    edit.event_type if current.kind is TriggerKind.EVENT else current.event_type
                ),
                created_at=current.created_at,
                updated_at=now,
            )
            if any(
                getattr(candidate, field) != getattr(current, field) for field in _DEFINING_FIELDS
            ):
                # `replace` re-runs the domain validation, so the bumped row is checked before the
                # write and an inconsistent revision can never reach the database.
                candidate = replace(candidate, config_revision=current.config_revision + 1)
            connection.execute(
                update(TriggerDefinitionRecord)
                .where(
                    TriggerDefinitionRecord.id == trigger_id,
                    TriggerDefinitionRecord.owner_user_id == owner_user_id,
                )
                .values(**self._column_values(candidate))
            )
            return self._read_definition(connection, owner_user_id, trigger_id)

        return self._runner.run(operation)

    def set_enabled(
        self,
        owner_user_id: int,
        trigger_id: int,
        enabled: bool,
        now: datetime,
        *,
        next_fire_at: datetime | None = None,
    ) -> TriggerDefinition:
        """Enable or disable, keeping `next_fire_at` consistent with `enabled`.

        Disabling clears the next fire time so the row cannot hold a stale instant, and enabling
        requires one, so the pairing the database enforces is exactly what this writes.
        """

        def operation(connection: Connection) -> TriggerDefinition:
            current = self._read_definition(connection, owner_user_id, trigger_id)
            if enabled:
                if current.kind in SCHEDULE_KINDS and next_fire_at is None:
                    raise TriggerNotEditable("enabling a schedule requires its next fire time")
                if current.kind not in SCHEDULE_KINDS and next_fire_at is not None:
                    raise TriggerNotEditable("a delivery-driven trigger has no next fire time")
                if current.kind is TriggerKind.ONE_TIME and self._has_occurrence(
                    connection, trigger_id
                ):
                    raise TriggerNotEditable("a completed one-time trigger cannot be re-enabled")
            connection.execute(
                update(TriggerDefinitionRecord)
                .where(
                    TriggerDefinitionRecord.id == trigger_id,
                    TriggerDefinitionRecord.owner_user_id == owner_user_id,
                )
                .values(
                    enabled=enabled,
                    next_fire_at=next_fire_at if enabled else None,
                    updated_at=now,
                )
            )
            return self._read_definition(connection, owner_user_id, trigger_id)

        return self._runner.run(operation)

    def delete_trigger(self, owner_user_id: int, trigger_id: int) -> None:
        """Delete only a trigger with no history, so occurrence evidence is never destroyed."""

        def operation(connection: Connection) -> None:
            self._read_definition(connection, owner_user_id, trigger_id)
            if self._has_occurrence(connection, trigger_id):
                raise TriggerHasHistory("a trigger with occurrences cannot be deleted")
            connection.execute(
                delete(TriggerDefinitionRecord).where(
                    TriggerDefinitionRecord.id == trigger_id,
                    TriggerDefinitionRecord.owner_user_id == owner_user_id,
                )
            )

        self._runner.run(operation)

    # ------------------------------------------------------------------------------------------
    # History and provenance
    # ------------------------------------------------------------------------------------------

    def list_occurrences(
        self, owner_user_id: int, trigger_id: int, limit: int, before_id: int | None
    ) -> tuple[TriggerOccurrence, ...]:
        self.get_trigger(owner_user_id, trigger_id)
        query = select(TriggerOccurrenceRecord).where(
            TriggerOccurrenceRecord.trigger_definition_id == trigger_id,
            TriggerOccurrenceRecord.owner_user_id == owner_user_id,
        )
        if before_id is not None:
            query = query.where(TriggerOccurrenceRecord.id < before_id)
        query = query.order_by(TriggerOccurrenceRecord.id.desc()).limit(limit)
        with self._engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        return tuple(self._to_occurrence(row) for row in rows)

    def occurrence_for_run(self, run_id: int) -> TriggerOccurrence | None:
        """Reverse provenance: the occurrence that explains this Run, or None for a manual Run.

        Descriptive only. Nothing that claims, starts, retries, recovers, cancels or terminalizes
        may consult this — a record that can influence execution is not descriptive.
        """
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(TriggerOccurrenceRecord).where(TriggerOccurrenceRecord.run_id == run_id)
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else self._to_occurrence(row)

    # ------------------------------------------------------------------------------------------
    # Atomic materialization
    # ------------------------------------------------------------------------------------------

    def materialize_occurrence_and_run(
        self, command: TriggerMaterializationCommand
    ) -> TriggerMaterializationOutcome:
        """One transaction: re-read the trigger, materialize an occurrence, and submit a Run.

        The trigger re-read is authoritative for owner, target, revision and input — the command
        deliberately carries none of them. When the Agent submission is authoritatively refused, a
        terminal `skipped` occurrence is written in the same transaction instead of inventing a Run.
        Admission backpressure is **not** such a refusal: it rolls the whole attempt back, so no
        occurrence is lost and the trigger is retried on a later tick.
        """
        return self._runner.run(lambda connection: self._materialize_once(connection, command))

    def _materialize_once(
        self, connection: Connection, command: TriggerMaterializationCommand
    ) -> TriggerMaterializationOutcome:
        """The one materialization core, in the one ordering every kind of trigger uses.

        The ordering is load-bearing, and it is why a second scheduler racing the first is
        harmless. **Identity is resolved before authority is applied**: if the deterministic
        occurrence this command names already exists, that row is the answer — whatever has since
        happened to `enabled`, `config_revision` or `next_fire_at` is beside the point, because the
        work this command describes has already been done and recorded. Applying authority first
        would turn a completed materialization into an error for whichever caller arrived second.

        Authority is still applied in full to every *new* identity: a fresh delivery to a disabled
        trigger is refused exactly as before. The reorder narrows nothing for a new occurrence; it
        only stops a duplicate from being mistaken for one.
        """
        definition = self._load_definition(connection, command.trigger_definition_id)
        self._require_identity_matches_kind(definition.kind, command)

        existing = self._find_existing(connection, definition, command)
        if existing is not None:
            # A duplicate identity refers to an occurrence that already exists. Nothing is written:
            # the original terminal outcome, whichever status it has, is authoritative.
            return TriggerMaterializationOutcome(occurrence=existing, duplicate=True)
        if not definition.enabled:
            raise TriggerNotEditable("a disabled trigger materializes nothing")

        return TriggerMaterializationOutcome(
            occurrence=self._materialize_new(connection, definition, command)
        )

    def _materialize_new(
        self,
        connection: Connection,
        definition: TriggerDefinition,
        command: TriggerMaterializationCommand,
        *,
        terminal_skip: SkipReason | None = None,
    ) -> TriggerOccurrence:
        """Create the Run and the occurrence for an identity that does not yet exist.

        `terminal_skip` short-circuits the Run entirely: a schedule that cannot be evaluated has no
        Run to create, but its occurrence is still owed, because "this schedule was due and could
        not be honoured" is a durable fact an operator needs.
        """
        if terminal_skip is not None:
            return self._write_skipped(connection, definition, command, terminal_skip)
        # The resolved Agent Definition was read before this transaction opened, so the durable row
        # is re-checked here against it. Without this, a drift between the two would be recorded as
        # an `agent_disabled` skip — consuming a scheduled occurrence, and telling the operator
        # something untrue, since the Agent is neither disabled nor at fault.
        self._require_agent_definition_unchanged(connection, definition, command)
        try:
            run = insert_run_and_job_on_connection(
                connection,
                owner_user_id=definition.owner_user_id,
                agent_instance_id=definition.agent_instance_id,
                input_text=definition.input_text,
                limits=command.definition.limits,
                definition_id=command.definition.definition_id,
                now=command.now,
                max_attempts=self.max_attempts,
                capacity=self._capacity,
                agent_capacity=self._agent_capacity,
                provider_capacity=self._provider_capacity,
            )
        except DurableSubmissionRejected:
            # The definition identity was verified immediately above, on this connection, inside
            # this write transaction, so the remaining way the canonical helper's predicates can
            # fail is that the Agent Instance is disabled. That is the frozen meaning of
            # `agent_disabled`, and it is the only meaning reachable here.
            return self._write_skipped(connection, definition, command, SkipReason.AGENT_DISABLED)
        return self._write_occurrence(
            connection,
            definition,
            command,
            status=OccurrenceStatus.RUN_CREATED,
            run_id=run.id,
            skip_code=None,
            skip_message=None,
        )

    @staticmethod
    def _require_agent_definition_unchanged(
        connection: Connection,
        definition: TriggerDefinition,
        command: TriggerMaterializationCommand,
    ) -> None:
        """Fail closed when the target Agent Instance no longer matches the resolved definition.

        The canonical helper enforces the same predicate, but it reports every refusal as one
        undifferentiated error. Checking here first is what lets the two refusals be told apart: a
        definition change is a race that must consume nothing, while a disabled Agent is an outcome
        that must be recorded.
        """
        expected = command.definition.definition_id
        row = (
            connection.execute(
                select(
                    AgentInstanceRecord.agent_key,
                    AgentInstanceRecord.agent_definition_version,
                ).where(AgentInstanceRecord.id == definition.agent_instance_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None or (
            str(row["agent_key"]),
            str(row["agent_definition_version"]),
        ) != (expected.agent_key, expected.agent_definition_version):
            raise TriggerNotEditable("the target agent instance changed definition")

    def _write_skipped(
        self,
        connection: Connection,
        definition: TriggerDefinition,
        command: TriggerMaterializationCommand,
        reason: SkipReason,
    ) -> TriggerOccurrence:
        """Record a recognised occurrence that produced no Run, with its static reason."""
        return self._write_occurrence(
            connection,
            definition,
            command,
            status=OccurrenceStatus.SKIPPED,
            run_id=None,
            skip_code=reason.value,
            skip_message=SKIP_MESSAGES[reason],
        )

    @staticmethod
    def _require_identity_matches_kind(
        kind: TriggerKind, command: TriggerMaterializationCommand
    ) -> None:
        """The occurrence's identity must be the one this kind of trigger actually dedupes on.

        The database cannot check this, because an occurrence deliberately carries no `kind` — so
        the transaction re-reads the definition and enforces the pairing here. Without it a
        schedule occurrence could be written with an `event_id`, and because SQLite treats NULLs as
        distinct under a UNIQUE index, that row would silently escape schedule dedupe entirely.
        """
        has_nominal = command.nominal_at is not None
        has_event = command.event_id is not None
        has_key = command.idempotency_key is not None
        if kind in SCHEDULE_KINDS:
            valid = has_nominal and not has_event and not has_key
        elif kind is TriggerKind.EVENT:
            valid = has_event and not has_nominal and not has_key
        else:
            # A keyed webhook dedupes on its key; a keyless one has no deterministic identity at
            # all and is never deduped, so carrying none of the three is correct for it.
            valid = (has_key and not has_nominal and not has_event) or not (
                has_nominal or has_event
            )
        if not valid:
            raise InvalidTrigger("occurrence identity does not match the trigger kind")

    # ------------------------------------------------------------------------------------------
    # The scheduler seam: due scanning and one verified, atomic schedule materialization
    # ------------------------------------------------------------------------------------------

    def due_schedule_candidates(
        self, *, now: datetime, limit: int, after: tuple[datetime, int] | None
    ) -> tuple[DueScheduleCandidate, ...]:
        """Read one bounded page of due schedules, in a stable total order.

        The scan is a read: it opens no write transaction, takes no lock, and is not authority. The
        order is `(next_fire_at, id)` so the page is resumable without skipping or repeating a row,
        and it is served entirely by the partial index `0008` already carries over those columns
        for rows that have a next fire time.

        `enabled = 1` is implied by the `next_fire_alignment` invariant — an enabled schedule
        always has a next fire time and a disabled one never does — but it is stated anyway, so the
        query says what it means rather than relying on a reader to reconstruct the implication. It
        is a residual filter either way, applied after the index seek, and the transaction re-checks
        it regardless.
        """
        query = (
            select(
                TriggerDefinitionRecord,
                AgentInstanceRecord.agent_key.label("agent_key"),
                AgentInstanceRecord.agent_definition_version.label("agent_definition_version"),
            )
            .join(
                AgentInstanceRecord,
                AgentInstanceRecord.id == TriggerDefinitionRecord.agent_instance_id,
            )
            .where(
                TriggerDefinitionRecord.enabled.is_(True),
                TriggerDefinitionRecord.next_fire_at.is_not(None),
                TriggerDefinitionRecord.next_fire_at <= now,
                TriggerDefinitionRecord.kind.in_([kind.value for kind in SCHEDULE_KINDS]),
            )
            .order_by(
                TriggerDefinitionRecord.next_fire_at.asc(),
                TriggerDefinitionRecord.id.asc(),
            )
            .limit(limit)
        )
        if after is not None:
            after_time, after_id = after
            query = query.where(
                or_(
                    TriggerDefinitionRecord.next_fire_at > after_time,
                    and_(
                        TriggerDefinitionRecord.next_fire_at == after_time,
                        TriggerDefinitionRecord.id > after_id,
                    ),
                )
            )
        with self._engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        return tuple(
            DueScheduleCandidate(
                trigger=self._to_definition(row),
                expected_config_revision=int(row["config_revision"]),
                expected_next_fire_at=_as_utc(row["next_fire_at"]),
            )
            for row in rows
        )

    def materialize_schedule_occurrence(
        self, command: ScheduleMaterializationCommand
    ) -> ScheduleMaterializationOutcome:
        """One transaction: verify a schedule decision against durable state, then apply it.

        Everything the decision was computed from is re-read here and required to be unchanged. A
        decision that no longer matches current state writes **nothing at all** — no occurrence, no
        Run, no Job, and no schedule mutation — because recording a race as history would make a
        harmless lost race permanent and indistinguishable from a real outcome.
        """
        return self._runner.run(
            lambda connection: self._materialize_schedule_once(connection, command)
        )

    def _materialize_schedule_once(
        self, connection: Connection, command: ScheduleMaterializationCommand
    ) -> ScheduleMaterializationOutcome:
        definition = self._load_definition(connection, command.trigger_definition_id)
        existing = self._find_existing_schedule(connection, definition, command.nominal_at)
        if existing is not None:
            # Identity before authority, for the same reason the shared core does it: this
            # occurrence already exists, so it is the answer whether or not the trigger is still
            # enabled. A one-time trigger is disabled by its own materialization, so without this
            # a second scheduler would report a failure for work that succeeded.
            return ScheduleMaterializationOutcome(
                kind=ScheduleOutcomeKind.DUPLICATED, occurrence=existing
            )
        stale = self._schedule_stale_reason(connection, definition, command)
        if stale is not None:
            return ScheduleMaterializationOutcome(
                kind=ScheduleOutcomeKind.STALE, stale_reason=stale
            )
        occurrence = self._materialize_new(
            connection,
            definition,
            TriggerMaterializationCommand(
                trigger_definition_id=definition.id,
                definition=command.definition,
                now=command.now,
                occurred_at=command.occurred_at,
                nominal_at=command.nominal_at,
            ),
            terminal_skip=command.terminal_skip,
        )
        self._apply_schedule_transition(connection, definition, command)
        kind = (
            ScheduleOutcomeKind.SKIPPED
            if occurrence.status is OccurrenceStatus.SKIPPED
            else ScheduleOutcomeKind.MATERIALIZED
        )
        return ScheduleMaterializationOutcome(kind=kind, occurrence=occurrence)

    def _schedule_stale_reason(
        self,
        connection: Connection,
        definition: TriggerDefinition,
        command: ScheduleMaterializationCommand,
    ) -> StaleReason | None:
        """Why this decision no longer applies, or `None` when it still does.

        Checked in the order that gives the most informative answer, and every check is a
        **precondition**: none of these values is written back. The revision and the next fire time
        are compared against what the scan saw, which is what makes a concurrent scheduler's
        committed advance stop this one — for a recurring trigger the identity alone would not,
        because two schedulers whose clocks differ can compute different nominal instants for the
        same trigger and the unique index would not separate them.
        """
        if not definition.is_schedule:
            return StaleReason.NOT_SCHEDULE
        if definition.config_revision != command.expected_config_revision:
            return StaleReason.REVISION_CHANGED
        if not definition.enabled:
            return StaleReason.DISABLED
        stored = definition.next_fire_at
        if stored is None or _as_utc(stored) != _as_utc(command.expected_next_fire_at):
            return StaleReason.NEXT_FIRE_CHANGED
        if _as_utc(stored) > _as_utc(command.now):
            return StaleReason.NOT_DUE
        # The Agent Definition is resolved outside the transaction, so the target's identity is
        # re-checked before anything is written. A change here is a race that must consume nothing.
        expected = command.definition.definition_id
        row = (
            connection.execute(
                select(
                    AgentInstanceRecord.agent_key,
                    AgentInstanceRecord.agent_definition_version,
                ).where(AgentInstanceRecord.id == definition.agent_instance_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None or (
            str(row["agent_key"]),
            str(row["agent_definition_version"]),
        ) != (expected.agent_key, expected.agent_definition_version):
            return StaleReason.AGENT_DEFINITION_CHANGED
        return None

    def _apply_schedule_transition(
        self,
        connection: Connection,
        definition: TriggerDefinition,
        command: ScheduleMaterializationCommand,
    ) -> None:
        """Move the trigger to its post-occurrence state, in the same transaction.

        A `None` next fire time completes the schedule and disables it, which is the only shape the
        `next_fire_alignment` invariant accepts for a finished schedule — a one-time trigger whose
        sole occurrence exists, or a schedule that can no longer be evaluated. Otherwise the next
        fire time advances and the trigger stays enabled, including when the occurrence was a skip:
        an Agent being temporarily disabled must not silently end a recurring schedule.
        """
        values: dict[str, object] = {"updated_at": command.now}
        if command.next_fire_at_after is None:
            values["enabled"] = False
            values["next_fire_at"] = None
        else:
            values["next_fire_at"] = command.next_fire_at_after
        connection.execute(
            update(TriggerDefinitionRecord)
            .where(TriggerDefinitionRecord.id == definition.id)
            .values(**values)
        )

    @staticmethod
    def _find_existing_schedule(
        connection: Connection, definition: TriggerDefinition, nominal_at: datetime
    ) -> TriggerOccurrence | None:
        row = (
            connection.execute(
                select(TriggerOccurrenceRecord)
                .where(
                    TriggerOccurrenceRecord.trigger_definition_id == definition.id,
                    TriggerOccurrenceRecord.nominal_at == nominal_at,
                )
                .order_by(TriggerOccurrenceRecord.id)
                .limit(1)
            )
            .mappings()
            .first()
        )
        return None if row is None else SqlAlchemyTriggerPersistence._to_occurrence(row)

    def _find_existing(
        self,
        connection: Connection,
        definition: TriggerDefinition,
        command: TriggerMaterializationCommand,
    ) -> TriggerOccurrence | None:
        query = select(TriggerOccurrenceRecord).where(
            TriggerOccurrenceRecord.trigger_definition_id == definition.id
        )
        if command.nominal_at is not None:
            query = query.where(TriggerOccurrenceRecord.nominal_at == command.nominal_at)
        elif command.event_id is not None:
            query = query.where(TriggerOccurrenceRecord.event_id == command.event_id)
        elif command.idempotency_key is not None:
            query = query.where(TriggerOccurrenceRecord.idempotency_key == command.idempotency_key)
        else:
            return None
        row = (
            connection.execute(query.order_by(TriggerOccurrenceRecord.id).limit(1))
            .mappings()
            .first()
        )
        return None if row is None else self._to_occurrence(row)

    def _write_occurrence(
        self,
        connection: Connection,
        definition: TriggerDefinition,
        command: TriggerMaterializationCommand,
        *,
        status: OccurrenceStatus,
        run_id: int | None,
        skip_code: str | None,
        skip_message: str | None,
    ) -> TriggerOccurrence:
        occurrence = TriggerOccurrence(
            id=0,
            trigger_definition_id=definition.id,
            # Owner, target and revision come from the re-read definition, never from the command:
            # they have exactly one authority, and it is the durable row.
            owner_user_id=definition.owner_user_id,
            agent_instance_id=definition.agent_instance_id,
            trigger_revision=definition.config_revision,
            status=status,
            run_id=run_id,
            skip_code=skip_code,
            skip_message=skip_message,
            nominal_at=command.nominal_at,
            event_id=command.event_id,
            idempotency_key=command.idempotency_key,
            payload_digest=command.payload_digest,
            payload_bytes=command.payload_bytes,
            occurred_at=command.occurred_at,
            created_at=command.now,
        )
        result = connection.execute(
            insert(TriggerOccurrenceRecord).values(
                trigger_definition_id=occurrence.trigger_definition_id,
                owner_user_id=occurrence.owner_user_id,
                agent_instance_id=occurrence.agent_instance_id,
                trigger_revision=occurrence.trigger_revision,
                status=occurrence.status.value,
                run_id=occurrence.run_id,
                skip_code=occurrence.skip_code,
                skip_message=occurrence.skip_message,
                nominal_at=occurrence.nominal_at,
                event_id=occurrence.event_id,
                idempotency_key=occurrence.idempotency_key,
                payload_digest=occurrence.payload_digest,
                payload_bytes=occurrence.payload_bytes,
                occurred_at=occurrence.occurred_at,
                created_at=occurrence.created_at,
            )
        )
        primary_key = result.inserted_primary_key
        if primary_key is None or primary_key[0] is None:
            raise PersistenceUnavailable
        return self._read_occurrence(connection, int(primary_key[0]))

    # ------------------------------------------------------------------------------------------
    # Reads and row mapping
    # ------------------------------------------------------------------------------------------

    @staticmethod
    def _require_owned_instance(
        connection: Connection, owner_user_id: int, agent_instance_id: int
    ) -> None:
        row = (
            connection.execute(
                select(AgentInstanceRecord.id).where(
                    AgentInstanceRecord.id == agent_instance_id,
                    AgentInstanceRecord.owner_user_id == owner_user_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            # A foreign Agent Instance is indistinguishable from a nonexistent one.
            raise TriggerNotFound(agent_instance_id)

    def _load_definition(self, connection: Connection, trigger_id: int) -> TriggerDefinition:
        row = (
            connection.execute(
                select(TriggerDefinitionRecord).where(TriggerDefinitionRecord.id == trigger_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise TriggerNotFound(trigger_id)
        return self._to_definition(row)

    def _read_definition(
        self, connection: Connection, owner_user_id: int, trigger_id: int
    ) -> TriggerDefinition:
        """Read one owner-scoped trigger, joining the target Agent's exact definition identity.

        The join supplies `agent_definition_id` so materialization can resolve the Agent Definition
        the same way a manual submission does. It is a derived, read-only projection and is never
        persisted on the trigger.
        """
        row = (
            connection.execute(
                select(
                    TriggerDefinitionRecord,
                    AgentInstanceRecord.agent_key.label("agent_key"),
                    AgentInstanceRecord.agent_definition_version.label("agent_definition_version"),
                )
                .join(
                    AgentInstanceRecord,
                    AgentInstanceRecord.id == TriggerDefinitionRecord.agent_instance_id,
                )
                .where(
                    TriggerDefinitionRecord.id == trigger_id,
                    TriggerDefinitionRecord.owner_user_id == owner_user_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise TriggerNotFound(trigger_id)
        return self._to_definition(row)

    def _read_occurrence(self, connection: Connection, occurrence_id: int) -> TriggerOccurrence:
        row = (
            connection.execute(
                select(TriggerOccurrenceRecord).where(TriggerOccurrenceRecord.id == occurrence_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise TriggerNotFound(occurrence_id)
        return self._to_occurrence(row)

    def _has_occurrence(self, connection: Connection, trigger_id: int) -> bool:
        count = connection.execute(
            select(func.count())
            .select_from(TriggerOccurrenceRecord)
            .where(TriggerOccurrenceRecord.trigger_definition_id == trigger_id)
        ).scalar_one()
        return bool(count)

    @staticmethod
    def _schedule_of(definition: TriggerDefinition) -> ScheduleSpec | None:
        """Reconstruct the schedule a stored trigger already carries.

        Used by an edit that does not restate its schedule, so a rename cannot silently erase a cron
        expression and leave the row's kind shape invalid. The reconstruction itself lives in the
        domain, so the edit path and the schedule evaluator cannot disagree about what a stored row
        means.
        """
        return schedule_of(definition)

    @staticmethod
    def _candidate(
        *,
        id: int,
        owner: int,
        agent_instance_id: int,
        kind: TriggerKind,
        display_name: str,
        enabled: bool,
        input_text: str,
        config_revision: int,
        misfire_policy: MisfirePolicy,
        schedule: ScheduleSpec | None,
        next_fire_at: datetime | None,
        public_id: str | None,
        secret_digest: bytes | None,
        secret_created_at: datetime | None,
        event_type: str | None,
        created_at: datetime,
        updated_at: datetime,
    ) -> TriggerDefinition:
        """Build the domain value a write is about to persist, so it validates before the write."""
        return TriggerDefinition(
            id=id,
            owner_user_id=owner,
            agent_instance_id=agent_instance_id,
            kind=kind,
            display_name=display_name,
            enabled=enabled,
            input_text=input_text,
            config_revision=config_revision,
            misfire_policy=misfire_policy,
            next_fire_at=next_fire_at,
            run_at=schedule.run_at if schedule is not None else None,
            interval_seconds=schedule.interval_seconds if schedule is not None else None,
            cron_expression=(
                schedule.cron_expression.text
                if schedule is not None and schedule.cron_expression is not None
                else None
            ),
            timezone=(
                schedule.timezone.name
                if schedule is not None and schedule.timezone is not None
                else None
            ),
            public_id=public_id,
            secret_digest=secret_digest,
            secret_created_at=secret_created_at,
            event_type=event_type,
            created_at=created_at,
            updated_at=updated_at,
        )

    @staticmethod
    def _to_definition(row: RowMapping) -> TriggerDefinition:
        keys = set(row.keys())
        agent_key = row["agent_key"] if "agent_key" in keys else None
        return TriggerDefinition(
            id=int(row["id"]),
            owner_user_id=int(row["owner_user_id"]),
            agent_instance_id=int(row["agent_instance_id"]),
            kind=TriggerKind(row["kind"]),
            display_name=str(row["display_name"]),
            enabled=bool(row["enabled"]),
            input_text=str(row["input_text"]),
            config_revision=int(row["config_revision"]),
            misfire_policy=MisfirePolicy(row["misfire_policy"]),
            next_fire_at=row["next_fire_at"],
            run_at=row["run_at"],
            interval_seconds=row["interval_seconds"],
            cron_expression=row["cron_expression"],
            timezone=row["timezone"],
            public_id=row["public_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            secret_digest=row["secret_digest"],
            secret_created_at=row["secret_created_at"],
            event_type=row["event_type"],
            agent_definition_id=(
                None
                if agent_key is None
                else AgentDefinitionId(str(agent_key), str(row["agent_definition_version"]))
            ),
        )

    @staticmethod
    def _to_occurrence(row: RowMapping) -> TriggerOccurrence:
        return TriggerOccurrence(
            id=int(row["id"]),
            trigger_definition_id=int(row["trigger_definition_id"]),
            owner_user_id=int(row["owner_user_id"]),
            agent_instance_id=int(row["agent_instance_id"]),
            trigger_revision=int(row["trigger_revision"]),
            status=OccurrenceStatus(row["status"]),
            run_id=row["run_id"],
            skip_code=row["skip_code"],
            skip_message=row["skip_message"],
            nominal_at=row["nominal_at"],
            event_id=row["event_id"],
            idempotency_key=row["idempotency_key"],
            occurred_at=row["occurred_at"],
            created_at=row["created_at"],
            payload_digest=row["payload_digest"],
            payload_bytes=row["payload_bytes"],
        )

    @staticmethod
    def _column_values(definition: TriggerDefinition) -> dict[str, object]:
        return {
            "owner_user_id": definition.owner_user_id,
            "agent_instance_id": definition.agent_instance_id,
            "kind": definition.kind.value,
            "display_name": definition.display_name,
            "enabled": definition.enabled,
            "input_text": definition.input_text,
            "config_revision": definition.config_revision,
            "misfire_policy": definition.misfire_policy.value,
            "next_fire_at": definition.next_fire_at,
            "run_at": definition.run_at,
            "interval_seconds": definition.interval_seconds,
            "cron_expression": definition.cron_expression,
            "timezone": definition.timezone,
            "public_id": definition.public_id,
            "secret_digest": definition.secret_digest,
            "secret_created_at": definition.secret_created_at,
            "event_type": definition.event_type,
            "created_at": definition.created_at,
            "updated_at": definition.updated_at,
        }


__all__ = ["SqlAlchemyTriggerPersistence"]
