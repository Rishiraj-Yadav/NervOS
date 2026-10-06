"""Durable autonomous workflows: executions, steps, checkpoints, signals, decisions.

ADR 0039. Additive only: no existing table is altered, so a database migrated to 0023
keeps every row and every execution audit it already had.
"""

import sqlalchemy as sa
from alembic import op

revision = "0024_durable_workflows"
down_revision = "0023_worker_sandbox_capability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_executions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False, primary_key=True),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "agent_instance_id",
            sa.Integer(),
            sa.ForeignKey("agent_instances.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("workflow_kind", sa.String(64), nullable=False),
        sa.Column("state_schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("submission_key", sa.String(64), nullable=False),
        sa.Column("submission_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("paused", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("checkpoint_revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("step_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_steps", sa.Integer(), server_default="32", nullable=False),
        sa.Column("model_call_reservation", sa.Integer(), server_default="256", nullable=False),
        sa.Column("tool_call_reservation", sa.Integer(), server_default="256", nullable=False),
        sa.Column(
            "output_token_reservation", sa.Integer(), server_default="262144", nullable=False
        ),
        sa.Column("reserved_model_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reserved_tool_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reserved_output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("deadline_hours", sa.Integer(), server_default="24", nullable=False),
        sa.Column("max_retained_checkpoints", sa.Integer(), server_default="128", nullable=False),
        sa.Column("deadline_at", sa.DateTime(), nullable=False),
        sa.Column("wait_kind", sa.String(24), nullable=True),
        sa.Column("wakeup_at", sa.DateTime(), nullable=True),
        sa.Column("signal_key", sa.String(64), nullable=True),
        sa.Column("decision_key", sa.String(64), nullable=True),
        sa.Column("review_reason", sa.String(64), nullable=True),
        sa.Column("agent_key", sa.String(128), nullable=False),
        sa.Column("agent_definition_version", sa.String(64), nullable=False),
        sa.Column("model_provider", sa.String(64), nullable=False),
        sa.Column("model_name", sa.String(256), nullable=False),
        sa.Column("package_content_digest", sa.String(64), nullable=True),
        sa.Column("package_environment_digest", sa.String(64), nullable=True),
        sa.Column("effective_config_digest", sa.String(64), nullable=True),
        sa.Column("agent_instance_config_revision", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','runnable','running','waiting','succeeded','failed',"
            "'cancelled','needs_review')",
            name="status_value",
        ),
        sa.CheckConstraint(
            "wait_kind IS NULL OR wait_kind IN ('time','signal','owner_decision')",
            name="wait_kind_value",
        ),
        sa.CheckConstraint(
            "(status = 'waiting' AND wait_kind IS NOT NULL "
            "AND (wait_kind <> 'time' OR wakeup_at IS NOT NULL)) "
            "OR (status <> 'waiting' AND wait_kind IS NULL AND wakeup_at IS NULL)",
            name="wait_shape",
        ),
        sa.CheckConstraint("checkpoint_revision >= 0", name="checkpoint_revision_nonnegative"),
        sa.CheckConstraint("step_count >= 0", name="step_count_nonnegative"),
        sa.CheckConstraint("1 <= max_steps AND max_steps <= 128", name="max_steps_bound"),
        sa.CheckConstraint(
            "1 <= model_call_reservation AND model_call_reservation <= 2048",
            name="model_reservation_bound",
        ),
        sa.CheckConstraint(
            "0 <= tool_call_reservation AND tool_call_reservation <= 2048",
            name="tool_reservation_bound",
        ),
        sa.CheckConstraint(
            "1 <= output_token_reservation AND output_token_reservation <= 2097152",
            name="output_reservation_bound",
        ),
        sa.CheckConstraint(
            "reserved_model_calls >= 0 AND reserved_tool_calls >= 0 "
            "AND reserved_output_tokens >= 0",
            name="reserved_nonnegative",
        ),
        sa.CheckConstraint(
            "reserved_model_calls <= model_call_reservation "
            "AND reserved_tool_calls <= tool_call_reservation "
            "AND reserved_output_tokens <= output_token_reservation",
            name="reserved_within_budget",
        ),
        sa.CheckConstraint("1 <= deadline_hours AND deadline_hours <= 168", name="deadline_bound"),
        sa.CheckConstraint(
            "1 <= max_retained_checkpoints AND max_retained_checkpoints <= 128",
            name="retention_bound",
        ),
        sa.CheckConstraint("state_schema_version = 1", name="state_schema_version_value"),
        sa.CheckConstraint(
            "length(submission_key) BETWEEN 1 AND 64 AND submission_key NOT GLOB '*[^a-z0-9_-]*'",
            name="submission_key_bound",
        ),
        sa.CheckConstraint(
            "length(submission_digest) = 64 AND submission_digest NOT GLOB '*[^0-9a-f]*'",
            name="submission_digest_shape",
        ),
        sa.CheckConstraint("length(workflow_kind) BETWEEN 1 AND 64", name="workflow_kind_bound"),
        sa.CheckConstraint("length(agent_key) BETWEEN 1 AND 128", name="agent_key_bound"),
        sa.CheckConstraint(
            "length(agent_definition_version) BETWEEN 1 AND 64", name="definition_version_bound"
        ),
        sa.CheckConstraint(
            "(status IN ('succeeded','failed','cancelled','needs_review') "
            "AND finished_at IS NOT NULL) "
            "OR (status NOT IN ('succeeded','failed','cancelled','needs_review') "
            "AND finished_at IS NULL)",
            name="terminal_shape",
        ),
        sa.CheckConstraint("deadline_at >= created_at", name="deadline_after_creation"),
        sa.UniqueConstraint("owner_user_id", "submission_key", name="owner_submission_key"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_workflow_executions_due",
        "workflow_executions",
        ["paused", "status", "wakeup_at", "id"],
    )
    op.create_index(
        "ix_workflow_executions_owner_id", "workflow_executions", ["owner_user_id", "id"]
    )
    op.create_index(
        "ix_workflow_executions_agent_instance_id",
        "workflow_executions",
        ["agent_instance_id", "id"],
    )

    op.create_table(
        "workflow_steps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False, primary_key=True),
        sa.Column(
            "workflow_id",
            sa.Integer(),
            sa.ForeignKey("workflow_executions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("runs.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("expected_checkpoint_revision", sa.Integer(), nullable=False),
        sa.Column("state_schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("state_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("summary", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed','cancelled')", name="status_value"
        ),
        sa.CheckConstraint("step_number > 0", name="step_number_positive"),
        sa.CheckConstraint(
            "expected_checkpoint_revision >= 0", name="expected_revision_nonnegative"
        ),
        sa.CheckConstraint("state_schema_version = 1", name="state_schema_version_value"),
        sa.CheckConstraint(
            "length(state_digest) = 64 AND state_digest NOT GLOB '*[^0-9a-f]*'",
            name="state_digest_shape",
        ),
        sa.CheckConstraint("length(CAST(state_json AS BLOB)) <= 65536", name="state_byte_bound"),
        sa.CheckConstraint("summary IS NULL OR length(summary) <= 512", name="summary_bound"),
        sa.CheckConstraint(
            "(status IN ('succeeded','failed','cancelled') AND finished_at IS NOT NULL) "
            "OR (status NOT IN ('succeeded','failed','cancelled') AND finished_at IS NULL)",
            name="terminal_shape",
        ),
        sa.UniqueConstraint("workflow_id", "step_number", name="workflow_step_number"),
        sa.UniqueConstraint("workflow_id", "run_id", name="workflow_run"),
        sa.UniqueConstraint("run_id", name="single_run_per_step"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_workflow_steps_workflow_id", "workflow_steps", ["workflow_id", "step_number"]
    )
    op.create_index("ix_workflow_steps_run_id", "workflow_steps", ["run_id"])

    op.create_table(
        "workflow_checkpoints",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False, primary_key=True),
        sa.Column(
            "workflow_id",
            sa.Integer(),
            sa.ForeignKey("workflow_executions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("state_schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("state_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("revision >= 0", name="revision_nonnegative"),
        sa.CheckConstraint("step_number > 0", name="step_number_positive"),
        sa.CheckConstraint("state_schema_version = 1", name="state_schema_version_value"),
        sa.CheckConstraint(
            "length(state_digest) = 64 AND state_digest NOT GLOB '*[^0-9a-f]*'",
            name="state_digest_shape",
        ),
        sa.CheckConstraint("length(CAST(state_json AS BLOB)) <= 65536", name="state_byte_bound"),
        sa.UniqueConstraint("workflow_id", "revision", name="workflow_revision"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_workflow_checkpoints_workflow_id",
        "workflow_checkpoints",
        ["workflow_id", "revision"],
    )

    op.create_table(
        "workflow_signals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False, primary_key=True),
        sa.Column(
            "workflow_id",
            sa.Integer(),
            sa.ForeignKey("workflow_executions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("signal_key", sa.String(64), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("signal_json", sa.Text(), nullable=False),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("expected_revision >= 0", name="expected_revision_nonnegative"),
        sa.CheckConstraint(
            "length(payload_digest) = 64 AND payload_digest NOT GLOB '*[^0-9a-f]*'",
            name="payload_digest_shape",
        ),
        sa.CheckConstraint("length(CAST(signal_json AS BLOB)) <= 8192", name="signal_byte_bound"),
        sa.CheckConstraint("length(signal_key) BETWEEN 1 AND 64", name="signal_key_bound"),
        sa.CheckConstraint(
            "accepted_at IS NULL OR accepted_at >= received_at", name="accepted_order"
        ),
        sa.UniqueConstraint("workflow_id", "signal_key", name="workflow_signal_key"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_workflow_signals_workflow_id", "workflow_signals", ["workflow_id", "id"])

    op.create_table(
        "workflow_decisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False, primary_key=True),
        sa.Column(
            "workflow_id",
            sa.Integer(),
            sa.ForeignKey("workflow_executions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("checkpoint_revision", sa.Integer(), nullable=False),
        sa.Column(
            "tool_definition_id",
            sa.Integer(),
            sa.ForeignKey("tool_definitions.id"),
            nullable=False,
        ),
        sa.Column("upstream_name", sa.String(128), nullable=False),
        sa.Column("action_fingerprint", sa.String(64), nullable=False),
        sa.Column("arguments_digest", sa.String(64), nullable=False),
        sa.Column("preview_json", sa.Text(), nullable=False),
        sa.Column("package_content_digest", sa.String(64), nullable=True),
        sa.Column("effective_config_digest", sa.String(64), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending','approved','denied','expired','cancelled','consumed')",
            name="state_value",
        ),
        sa.CheckConstraint("checkpoint_revision >= 0", name="checkpoint_revision_nonnegative"),
        sa.CheckConstraint(
            "length(action_fingerprint) = 64 AND action_fingerprint NOT GLOB '*[^0-9a-f]*'",
            name="fingerprint_shape",
        ),
        sa.CheckConstraint(
            "length(arguments_digest) = 64 AND arguments_digest NOT GLOB '*[^0-9a-f]*'",
            name="arguments_digest_shape",
        ),
        sa.CheckConstraint(
            "length(CAST(preview_json AS BLOB)) <= 65536", name="preview_byte_bound"
        ),
        sa.CheckConstraint("expires_at > requested_at", name="expiry_after_request"),
        sa.CheckConstraint(
            "(state = 'pending' AND decided_at IS NULL AND consumed_at IS NULL) "
            "OR (state = 'approved' AND decided_at IS NOT NULL AND consumed_at IS NULL) "
            "OR (state = 'consumed' AND decided_at IS NOT NULL AND consumed_at IS NOT NULL) "
            "OR (state IN ('denied','expired','cancelled') AND consumed_at IS NULL)",
            name="lifecycle_shape",
        ),
        sa.UniqueConstraint(
            "workflow_id",
            "checkpoint_revision",
            "tool_definition_id",
            name="workflow_revision_tool",
        ),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_workflow_decisions_owner_state",
        "workflow_decisions",
        ["owner_user_id", "state", "id"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "downgrade is refused: it would destroy durable workflow execution evidence, "
        "including checkpoints and owner decisions, which are append-only audit records"
    )
