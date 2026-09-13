# ruff: noqa: E501
"""Add Agent Instance and immutable-snapshot Run persistence.

Revision ID: 0002_stage_b1_agent_instances_runs
Revises: 0001_stage_a
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_stage_b1_agent_instances_runs"
down_revision: str | None = "0001_stage_a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_instances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("agent_key", sa.String(128), nullable=False),
        sa.Column("agent_definition_version", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("model_provider", sa.String(64), nullable=False),
        sa.Column("model_name", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False),
        sa.CheckConstraint("owner_user_id > 0", name="owner_positive"),
        sa.CheckConstraint("length(agent_key) BETWEEN 1 AND 128", name="agent_key_length"),
        sa.CheckConstraint(
            "length(agent_definition_version) BETWEEN 1 AND 64", name="definition_version_length"
        ),
        sa.CheckConstraint(
            "display_name = trim(display_name) AND length(display_name) BETWEEN 1 AND 100",
            name="display_name_shape",
        ),
        sa.CheckConstraint("length(CAST(display_name AS BLOB)) <= 400", name="display_name_bytes"),
        sa.CheckConstraint(
            "length(model_provider) BETWEEN 1 AND 64 AND model_provider = lower(model_provider)",
            name="provider_shape",
        ),
        sa.CheckConstraint(
            "model_name = trim(model_name) AND length(model_name) BETWEEN 1 AND 256",
            name="model_name_shape",
        ),
        sa.CheckConstraint("length(CAST(model_name AS BLOB)) <= 1024", name="model_name_bytes"),
        sa.CheckConstraint("enabled IN (0, 1)", name="enabled_boolean"),
        sa.CheckConstraint("updated_at >= created_at", name="timestamp_order"),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name="fk_agent_instances_owner_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_instances"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_agent_instances_owner_user_id_id",
        "agent_instances",
        ["owner_user_id", "id"],
        unique=False,
    )
    op.create_table(
        "runs",
        *[sa.Column(name, sa.Integer(), nullable=False) for name in ("id", "agent_instance_id")],
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("agent_key", sa.String(128), nullable=False),
        sa.Column("agent_definition_version", sa.String(64), nullable=False),
        sa.Column("model_provider", sa.String(64), nullable=False),
        sa.Column("model_name", sa.String(256), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        *[
            sa.Column(name, sa.Integer(), nullable=False)
            for name in (
                "input_max_bytes",
                "input_max_code_points",
                "output_max_bytes",
                "output_max_code_points",
                "provider_timeout_ms",
                "max_output_tokens",
                "max_model_calls",
            )
        ],
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column("finish_reason", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(512), nullable=True),
        *[
            sa.Column(name, sa.Integer(), nullable=True)
            for name in ("input_tokens", "output_tokens", "total_tokens", "elapsed_ms")
        ],
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=False), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=False), nullable=True),
        sa.CheckConstraint("agent_instance_id > 0", name="agent_instance_positive"),
        sa.CheckConstraint(
            "status IN ('created','running','succeeded','failed')", name="status_value"
        ),
        sa.CheckConstraint(
            "input_max_bytes > 0 AND input_max_code_points > 0 AND output_max_bytes > 0 AND output_max_code_points > 0 AND provider_timeout_ms > 0 AND max_output_tokens > 0 AND max_model_calls > 0",
            name="limits_positive",
        ),
        sa.CheckConstraint(
            "length(input_text) BETWEEN 1 AND input_max_code_points AND length(CAST(input_text AS BLOB)) <= input_max_bytes AND instr(input_text, char(0)) = 0 AND length(trim(input_text, char(9,10,11,12,13,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288))) > 0",
            name="input_bounds",
        ),
        sa.CheckConstraint(
            "output_text IS NULL OR (length(output_text) BETWEEN 1 AND output_max_code_points AND length(CAST(output_text AS BLOB)) <= output_max_bytes AND instr(output_text, char(0)) = 0)",
            name="output_bounds",
        ),
        sa.CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) AND (output_tokens IS NULL OR output_tokens >= 0) AND (total_tokens IS NULL OR total_tokens >= 0)",
            name="usage_nonnegative",
        ),
        sa.CheckConstraint("elapsed_ms IS NULL OR elapsed_ms >= 0", name="elapsed_nonnegative"),
        sa.CheckConstraint("started_at IS NULL OR started_at >= created_at", name="started_order"),
        sa.CheckConstraint(
            "finished_at IS NULL OR (started_at IS NOT NULL AND finished_at >= started_at)",
            name="finished_order",
        ),
        sa.CheckConstraint(
            "(status='created' AND started_at IS NULL AND finished_at IS NULL AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NULL AND error_message IS NULL AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL AND elapsed_ms IS NULL) OR (status='running' AND started_at IS NOT NULL AND finished_at IS NULL AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NULL AND error_message IS NULL AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL AND elapsed_ms IS NULL) OR (status='succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL AND output_text IS NOT NULL AND length(trim(output_text, char(9,10,11,12,13,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288))) > 0 AND error_code IS NULL AND error_message IS NULL AND elapsed_ms IS NOT NULL) OR (status='failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NOT NULL AND error_message IS NOT NULL AND length(trim(error_message, char(9,10,11,12,13,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288))) > 0 AND elapsed_ms IS NOT NULL)",
            name="lifecycle_shape",
        ),
        sa.ForeignKeyConstraint(
            ["agent_instance_id"],
            ["agent_instances.id"],
            name="fk_runs_agent_instance_id_agent_instances",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runs"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_runs_agent_instance_id_id", "runs", ["agent_instance_id", "id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_runs_agent_instance_id_id", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_agent_instances_owner_user_id_id", table_name="agent_instances")
    op.drop_table("agent_instances")
