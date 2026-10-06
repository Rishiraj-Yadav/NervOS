"""Approved runtime integration: policies, pinned context/bindings and memory suggestions."""

import sqlalchemy as sa
from alembic import op

revision = "0015_runtime_integration"
down_revision = "0014_stage_i4_marketplace_install_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_memory_policies",
        sa.Column(
            "agent_instance_id", sa.Integer(), sa.ForeignKey("agent_instances.id"), primary_key=True
        ),
        sa.Column("mode", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("extraction_enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("mode IN ('manual','review','automatic_private')", name="mode_value"),
        sa.CheckConstraint("revision > 0", name="revision_positive"),
    )
    op.create_table(
        "agent_tool_bindings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "agent_instance_id", sa.Integer(), sa.ForeignKey("agent_instances.id"), nullable=False
        ),
        sa.Column("alias", sa.String(128), nullable=False),
        sa.Column("package_id", sa.String(128), nullable=False),
        sa.Column("package_version", sa.String(64), nullable=False),
        sa.Column(
            "tool_definition_id", sa.Integer(), sa.ForeignKey("tool_definitions.id"), nullable=False
        ),
        sa.Column("reviewed_fingerprint", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("agent_instance_id", "alias", name="instance_alias"),
        sa.CheckConstraint("length(alias) BETWEEN 1 AND 128", name="alias_bound"),
    )
    op.create_table(
        "run_integrations",
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("context_json", sa.Text()),
        sa.Column("bindings_json", sa.Text(), nullable=False),
        sa.Column("memory_mode", sa.String(24), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("extraction_enabled", sa.Boolean(), nullable=False),
        sa.Column("proposals_json", sa.Text(), nullable=False),
        sa.Column("memory_state", sa.String(24), nullable=False),
        sa.Column("extraction_run_id", sa.Integer(), sa.ForeignKey("runs.id")),
    )
    op.create_table(
        "memory_suggestions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "agent_instance_id", sa.Integer(), sa.ForeignKey("agent_instances.id"), nullable=False
        ),
        sa.Column("source_run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_digest", sa.LargeBinary(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("memory_item_id", sa.Integer(), sa.ForeignKey("memory_items.id")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("source_run_id", "scope", "content_digest", name="source_fact"),
        sa.CheckConstraint("scope IN ('user','agent')", name="scope_value"),
        sa.CheckConstraint(
            "state IN ('pending','saved','dismissed','duplicate','quota')", name="state_value"
        ),
        sa.CheckConstraint(
            "length(CAST(content AS BLOB)) BETWEEN 1 AND 2000", name="content_bound"
        ),
    )
    op.create_index("ix_memory_suggestions_owner_id", "memory_suggestions", ["owner_user_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_memory_suggestions_owner_id", table_name="memory_suggestions")
    for name in (
        "memory_suggestions",
        "run_integrations",
        "agent_tool_bindings",
        "agent_memory_policies",
    ):
        op.drop_table(name)
