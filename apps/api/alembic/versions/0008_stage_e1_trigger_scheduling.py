# ruff: noqa: E501
"""stage e1 trigger definitions and immutable occurrences

Revision ID: 0008_stage_e1_trigger_scheduling
Revises: 0007_stage_d1_tool_capability_audit
Create Date: 2026-09-19 00:00:00.000000

E1 persists trigger configuration and terminal occurrence history. It does not add an execution
queue, scheduler lease, continuation job, or any Run execution primitive.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_stage_e1_trigger_scheduling"
down_revision = "0007_stage_d1_tool_capability_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trigger_definitions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("agent_instance_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("display_name", sa.String(length=400), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("config_revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("misfire_policy", sa.String(length=16), nullable=False),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("interval_seconds", sa.Integer(), nullable=True),
        sa.Column("cron_expression", sa.String(length=128), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("public_id", sa.String(length=64), nullable=True),
        sa.Column("secret_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("secret_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_instance_id"], ["agent_instances.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "kind IN ('one_time','interval','cron','webhook','event')", name="kind_value"
        ),
        sa.CheckConstraint("misfire_policy IN ('coalesce_one')", name="misfire_policy_value"),
        sa.CheckConstraint("enabled IN (0, 1)", name="enabled_value"),
        sa.CheckConstraint("config_revision > 0", name="config_revision_positive"),
        sa.CheckConstraint(
            "length(display_name) BETWEEN 1 AND 400 AND instr(display_name, char(0)) = 0",
            name="display_name_shape",
        ),
        sa.CheckConstraint(
            "length(input_text) BETWEEN 1 AND 4000 AND length(CAST(input_text AS BLOB)) <= 8000 "
            "AND instr(input_text, char(0)) = 0 AND length(trim(input_text, "
            "char(9,10,11,12,13,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,"
            "8200,8201,8202,8232,8233,8239,8287,12288))) > 0",
            name="input_bounds",
        ),
        # The single constraint that makes an illegal trigger unrepresentable. Exactly one of the
        # five branches holds, so a webhook cannot carry an event type and an interval cannot carry
        # a timezone. It is exhaustive rather than a set of pairwise exclusions, because a partial
        # rule would leave some cross-kind combination legal.
        sa.CheckConstraint(
            "((kind = 'one_time' AND run_at IS NOT NULL AND interval_seconds IS NULL "
            "AND cron_expression IS NULL AND timezone IS NULL AND public_id IS NULL "
            "AND secret_digest IS NULL AND secret_created_at IS NULL AND event_type IS NULL) OR "
            "(kind = 'interval' AND run_at IS NULL AND interval_seconds IS NOT NULL "
            "AND cron_expression IS NULL AND timezone IS NULL AND public_id IS NULL "
            "AND secret_digest IS NULL AND secret_created_at IS NULL AND event_type IS NULL) OR "
            "(kind = 'cron' AND run_at IS NULL AND interval_seconds IS NULL "
            "AND cron_expression IS NOT NULL AND timezone IS NOT NULL AND public_id IS NULL "
            "AND secret_digest IS NULL AND secret_created_at IS NULL AND event_type IS NULL) OR "
            "(kind = 'webhook' AND run_at IS NULL AND interval_seconds IS NULL "
            "AND cron_expression IS NULL AND timezone IS NULL AND public_id IS NOT NULL "
            "AND secret_digest IS NOT NULL AND secret_created_at IS NOT NULL AND event_type IS NULL "
            "AND next_fire_at IS NULL) OR "
            "(kind = 'event' AND run_at IS NULL AND interval_seconds IS NULL "
            "AND cron_expression IS NULL AND timezone IS NULL AND public_id IS NULL "
            "AND secret_digest IS NULL AND secret_created_at IS NULL AND event_type IS NOT NULL "
            "AND next_fire_at IS NULL))",
            name="kind_shape",
        ),
        # An enabled schedule always has a next fire time and a disabled one never does. Both
        # terminal paths converge on "disabled" -- a one-time trigger whose sole occurrence has been
        # materialized, and a cron trigger whose stored timezone became unresolvable -- so the rule
        # has no exception and a stale value is a database error rather than a state to reason about.
        sa.CheckConstraint(
            "((kind IN ('one_time','interval','cron') AND "
            "((enabled = 1 AND next_fire_at IS NOT NULL) OR "
            "(enabled = 0 AND next_fire_at IS NULL))) OR "
            "(kind IN ('webhook','event') AND next_fire_at IS NULL))",
            name="next_fire_alignment",
        ),
        sa.CheckConstraint(
            "interval_seconds IS NULL OR interval_seconds BETWEEN 60 AND 31536000",
            name="interval_bounds",
        ),
        sa.CheckConstraint(
            "cron_expression IS NULL OR (length(cron_expression) BETWEEN 1 AND 128 "
            "AND instr(cron_expression, char(0)) = 0)",
            name="cron_expression_shape",
        ),
        sa.CheckConstraint(
            "timezone IS NULL OR (length(timezone) BETWEEN 1 AND 64 "
            "AND instr(timezone, char(0)) = 0)",
            name="timezone_shape",
        ),
        # The locator is exactly 22 URL-safe characters, not "some bounded string": the domain and
        # the database agree on the shape the generator actually produces.
        sa.CheckConstraint(
            "public_id IS NULL OR (length(public_id) = 22 AND "
            "public_id NOT GLOB '*[^A-Za-z0-9_-]*' AND public_id GLOB '[A-Za-z0-9_-]*' "
            "AND instr(public_id, char(0)) = 0)",
            name="public_id_shape",
        ),
        sa.CheckConstraint(
            "secret_digest IS NULL OR length(secret_digest) = 32", name="secret_digest_shape"
        ),
        sa.CheckConstraint(
            "(secret_digest IS NULL) = (secret_created_at IS NULL)", name="secret_pair"
        ),
        # The strongest event-type subset SQLite can express. The remainder of the frozen grammar --
        # no empty segment, no reserved-namespace publication -- is enforced by the domain
        # validator, which is the authority; the persistence tests state that split rather than
        # claiming the database enforces all of it.
        sa.CheckConstraint(
            "event_type IS NULL OR (length(event_type) BETWEEN 1 AND 64 "
            "AND instr(event_type, char(0)) = 0 AND event_type = lower(event_type) "
            "AND event_type NOT GLOB '*[^a-z0-9_.]*' AND event_type NOT GLOB '.*' "
            "AND event_type NOT GLOB '*.' AND event_type NOT GLOB '*..*' "
            "AND event_type NOT LIKE 'nervos.%' AND event_type GLOB '[a-z]*')",
            name="event_type_shape",
        ),
        sa.CheckConstraint("updated_at >= created_at", name="timestamp_order"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_trigger_definitions_owner_user_id_id",
        "trigger_definitions",
        ["owner_user_id", "id"],
    )
    op.create_index(
        "ix_trigger_definitions_agent_instance_id_id",
        "trigger_definitions",
        ["agent_instance_id", "id"],
    )
    op.create_index(
        "ix_trigger_definitions_next_fire_at_id",
        "trigger_definitions",
        ["next_fire_at", "id"],
        sqlite_where=sa.text("next_fire_at IS NOT NULL"),
    )
    op.create_index(
        "ix_trigger_definitions_event_lookup",
        "trigger_definitions",
        ["owner_user_id", "event_type"],
        sqlite_where=sa.text("event_type IS NOT NULL"),
    )
    op.create_index(
        "ux_trigger_definitions_public_id",
        "trigger_definitions",
        ["public_id"],
        unique=True,
        sqlite_where=sa.text("public_id IS NOT NULL"),
    )

    op.create_table(
        "trigger_occurrences",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trigger_definition_id", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("agent_instance_id", sa.Integer(), nullable=False),
        sa.Column("trigger_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("skip_code", sa.String(length=64), nullable=True),
        sa.Column("skip_message", sa.String(length=512), nullable=True),
        sa.Column("nominal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("payload_digest", sa.LargeBinary(length=32), nullable=True),
        sa.Column("payload_bytes", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_instance_id"], ["agent_instances.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["trigger_definition_id"], ["trigger_definitions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status IN ('run_created','skipped')", name="status_value"),
        sa.CheckConstraint("trigger_revision > 0", name="trigger_revision_positive"),
        sa.CheckConstraint(
            "((status = 'run_created' AND run_id IS NOT NULL AND skip_code IS NULL "
            "AND skip_message IS NULL) OR (status = 'skipped' AND run_id IS NULL "
            "AND skip_code IS NOT NULL AND skip_message IS NOT NULL))",
            name="status_shape",
        ),
        sa.CheckConstraint(
            "skip_code IS NULL OR (length(skip_code) BETWEEN 1 AND 64 AND "
            "length(skip_message) BETWEEN 1 AND 512 AND instr(skip_message, char(0)) = 0 AND "
            "length(trim(skip_message, "
            "char(9,10,11,12,13,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,"
            "8200,8201,8202,8232,8233,8239,8287,12288))) > 0)",
            name="skip_bounds",
        ),
        sa.CheckConstraint(
            "payload_bytes IS NULL OR payload_bytes >= 0", name="payload_bytes_nonnegative"
        ),
        sa.CheckConstraint(
            "(payload_digest IS NULL) = (payload_bytes IS NULL)", name="payload_pair"
        ),
        sa.CheckConstraint(
            "payload_digest IS NULL OR length(payload_digest) = 32", name="payload_digest_shape"
        ),
        sa.CheckConstraint(
            "event_id IS NULL OR (length(event_id) BETWEEN 1 AND 128 "
            "AND instr(event_id, char(0)) = 0)",
            name="event_id_shape",
        ),
        sa.CheckConstraint(
            "idempotency_key IS NULL OR (length(idempotency_key) BETWEEN 1 AND 128 "
            "AND instr(idempotency_key, char(0)) = 0)",
            name="idempotency_key_shape",
        ),
        # At most one dedupe identity per row. All three being absent is the keyless webhook case,
        # which has no deterministic identity by design. This is what stops a schedule occurrence
        # from being written with a NULL instant -- and because SQLite treats NULLs as distinct
        # under a UNIQUE index, such a row would silently escape dedupe entirely.
        sa.CheckConstraint(
            "((nominal_at IS NOT NULL AND event_id IS NULL AND idempotency_key IS NULL) OR "
            "(nominal_at IS NULL AND event_id IS NOT NULL AND idempotency_key IS NULL) OR "
            "(nominal_at IS NULL AND event_id IS NULL AND idempotency_key IS NOT NULL) OR "
            "(nominal_at IS NULL AND event_id IS NULL AND idempotency_key IS NULL))",
            name="identity_shape",
        ),
        sa.CheckConstraint("created_at >= occurred_at", name="occurred_order"),
        sqlite_autoincrement=True,
    )
    for name, columns in (
        ("ix_trigger_occurrences_trigger_definition_id_id", ["trigger_definition_id", "id"]),
        ("ix_trigger_occurrences_owner_user_id_id", ["owner_user_id", "id"]),
    ):
        op.create_index(name, "trigger_occurrences", columns)
    op.create_index(
        "ux_trigger_occurrences_run_id",
        "trigger_occurrences",
        ["run_id"],
        unique=True,
        sqlite_where=sa.text("run_id IS NOT NULL"),
    )
    op.create_index(
        "ux_trigger_occurrences_schedule_identity",
        "trigger_occurrences",
        ["trigger_definition_id", "nominal_at"],
        unique=True,
        sqlite_where=sa.text("nominal_at IS NOT NULL"),
    )
    op.create_index(
        "ux_trigger_occurrences_event_identity",
        "trigger_occurrences",
        ["trigger_definition_id", "event_id"],
        unique=True,
        sqlite_where=sa.text("event_id IS NOT NULL"),
    )
    op.create_index(
        "ux_trigger_occurrences_idempotency_identity",
        "trigger_occurrences",
        ["trigger_definition_id", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )


#: Stage-E durable state. Every row counts, including a disabled trigger and a skipped occurrence:
#: a disabled trigger is still configuration a user authored, and a skip is a fact the user may
#: already have seen. The check runs before any DDL, so a refusal can never leave a half-dropped
#: schema behind.
DOWNGRADE_PREFLIGHT = (
    ("trigger definitions exist", "SELECT count(*) FROM trigger_definitions"),
    ("trigger occurrences exist", "SELECT count(*) FROM trigger_occurrences"),
)


def refuse_if_stage_e_state_exists() -> None:
    """Refuse a destructive downgrade while any Stage-E durable state would be lost.

    `run_id` is deliberately not a separate item: a Run identifier can only exist on an occurrence
    row, and any occurrence row already refuses. `runs` carries no Stage-E column and this
    downgrade never touches it.
    """
    connection = op.get_bind()
    blockers = [
        f"{reason} ({connection.scalar(sa.text(statement))})"
        for reason, statement in DOWNGRADE_PREFLIGHT
        if connection.scalar(sa.text(statement))
    ]
    if blockers:
        raise RuntimeError(
            "refusing to drop the Stage E schema while durable state exists: "
            + "; ".join(blockers)
            + ". Disable the automations and export their history first, or restore from backup."
        )


def downgrade() -> None:
    """Drop the two Stage E tables, refusing to destroy trigger or occurrence state."""
    refuse_if_stage_e_state_exists()
    op.drop_table("trigger_occurrences")
    op.drop_table("trigger_definitions")
