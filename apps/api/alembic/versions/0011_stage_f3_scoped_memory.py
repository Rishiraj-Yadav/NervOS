# ruff: noqa: E501
"""stage f3 scoped memory and version foundation

Revision ID: 0011_stage_f3_scoped_memory
Revises: 0010_stage_f2_context_snapshots_and_compactions
Create Date: 2026-09-22

F3 implements durable USER and AGENT scoped memory items, immutable versions,
provenance tracking, and context snapshot memory extensions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0011_stage_f3_scoped_memory"
down_revision = "0010_stage_f2_context_snapshots_and_compactions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add memory fields to run_context_snapshots
    op.add_column(
        "run_context_snapshots",
        sa.Column(
            "memory_items_json",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "run_context_snapshots",
        sa.Column("injected_user_memory_text", sa.Text(), nullable=True),
    )
    op.add_column(
        "run_context_snapshots",
        sa.Column("injected_agent_memory_text", sa.Text(), nullable=True),
    )

    # 2. memory_items
    op.create_table(
        "memory_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("agent_instance_id", sa.Integer(), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "current_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["agent_instance_id"], ["agent_instances.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("scope IN ('user','agent')", name="scope_value"),
        sa.CheckConstraint(
            "(scope = 'user' AND agent_instance_id IS NULL) OR (scope = 'agent' AND agent_instance_id IS NOT NULL)",
            name="scope_agent_shape",
        ),
        sa.CheckConstraint("status IN ('active','deleted')", name="status_value"),
        sa.CheckConstraint("current_version > 0", name="current_version_positive"),
        sa.CheckConstraint("updated_at >= created_at", name="timestamp_order"),
    )
    op.create_index(
        "ix_memory_items_owner_scope_status",
        "memory_items",
        ["owner_user_id", "scope", "status", "id"],
        unique=False,
    )
    op.create_index(
        "ix_memory_items_owner_agent_status",
        "memory_items",
        ["owner_user_id", "agent_instance_id", "status", "id"],
        unique=False,
        sqlite_where=text("agent_instance_id IS NOT NULL"),
    )

    # 3. memory_versions
    op.create_table(
        "memory_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("memory_item_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("provenance_type", sa.String(length=32), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["memory_item_id"], ["memory_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("memory_item_id", "version", name="uq_memory_versions_item_version"),
        sa.CheckConstraint("version > 0", name="version_positive"),
        sa.CheckConstraint(
            "length(content) >= 1 AND length(CAST(content AS BLOB)) <= 32000 AND instr(content, char(0)) = 0 AND length(trim(content, char(9,10,11,12,13,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288))) > 0",
            name="content_bounds",
        ),
        sa.CheckConstraint("length(content_digest) = 32", name="content_digest_length"),
        sa.CheckConstraint(
            "(source_kind = 'direct_user' AND source_id IS NULL) OR (source_kind IN ('promoted_message','promoted_run') AND source_id IS NOT NULL)",
            name="source_shape",
        ),
        sa.CheckConstraint(
            "source_kind IN ('direct_user','promoted_message','promoted_run')",
            name="source_kind_value",
        ),
        sa.CheckConstraint(
            "provenance_type IN ('user_authored','user_approved_inferred')",
            name="provenance_type_value",
        ),
    )
    op.create_index(
        "ix_memory_versions_item_version",
        "memory_versions",
        ["memory_item_id", "version"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("memory_versions")
    op.drop_table("memory_items")
    op.drop_column("run_context_snapshots", "injected_agent_memory_text")
    op.drop_column("run_context_snapshots", "injected_user_memory_text")
    op.drop_column("run_context_snapshots", "memory_items_json")
