# ruff: noqa: E501
"""stage f1 durable conversations, turns, messages, and run links

Revision ID: 0009_stage_f1_conversations
Revises: 0008_stage_e1_trigger_scheduling
Create Date: 2026-09-21

F1 persists the conversation execution protocol: Conversations, Turns, Messages, and
Run links. It adds no ContextBuilder, snapshots, compaction, or memory tables.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0009_stage_f1_conversations"
down_revision = "0008_stage_e1_trigger_scheduling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # conversations
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("agent_instance_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=400), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_instance_id"], ["agent_instances.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "title IS NULL OR (length(title) BETWEEN 1 AND 400 AND instr(title, char(0)) = 0)",
            name="title_shape",
        ),
        sa.CheckConstraint("updated_at >= created_at", name="timestamp_order"),
    )
    op.create_index(
        "ix_conversations_owner_id",
        "conversations",
        ["owner_user_id", "id"],
        unique=False,
    )
    op.create_index(
        "ix_conversations_owner_agent",
        "conversations",
        ["owner_user_id", "agent_instance_id"],
        unique=False,
    )

    # conversation_turns
    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("client_message_id", sa.String(length=128), nullable=False),
        sa.Column("content_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("authoritative_run_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["authoritative_run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("sequence > 0", name="sequence_positive"),
        sa.CheckConstraint(
            "state IN ('pending','running','succeeded','failed','cancelled','ambiguous')",
            name="state_value",
        ),
        sa.CheckConstraint(
            "length(client_message_id) BETWEEN 1 AND 128 AND instr(client_message_id, char(0)) = 0",
            name="client_message_id_shape",
        ),
        sa.CheckConstraint(
            "length(content_digest) = 32",
            name="content_digest_length",
        ),
        sa.UniqueConstraint("conversation_id", "sequence", name="uq_conversation_turns_sequence"),
        sa.UniqueConstraint(
            "conversation_id", "client_message_id", name="uq_conversation_turns_client_message_id"
        ),
    )
    op.create_index(
        "uq_conversation_turns_one_active",
        "conversation_turns",
        ["conversation_id"],
        unique=True,
        sqlite_where=text("state IN ('pending','running')"),
    )
    op.create_index(
        "ix_conversation_turns_conversation_sequence",
        "conversation_turns",
        ["conversation_id", "sequence"],
        unique=False,
    )

    # conversation_run_links
    op.create_table(
        "conversation_run_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("turn_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["turn_id"], ["conversation_turns.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("ordinal >= 1", name="ordinal_positive"),
        sa.CheckConstraint("role IN ('initial','retry')", name="link_role_value"),
        sa.UniqueConstraint("run_id", name="uq_conversation_run_links_run"),
        sa.UniqueConstraint("turn_id", "ordinal", name="uq_conversation_run_links_turn_ordinal"),
        sa.UniqueConstraint("turn_id", "run_id", name="uq_conversation_run_links_turn_run"),
    )

    # conversation_messages
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("turn_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_run_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["turn_id", "source_run_id"],
            ["conversation_run_links.turn_id", "conversation_run_links.run_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["turn_id"], ["conversation_turns.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("role IN ('user','assistant')", name="role_value"),
        sa.CheckConstraint(
            "length(content) BETWEEN 1 AND 32000 AND length(CAST(content AS BLOB)) <= 32000 AND instr(content, char(0)) = 0",
            name="message_content_bounds",
        ),
        sa.UniqueConstraint("turn_id", "role", name="uq_conversation_messages_turn_role"),
        sa.CheckConstraint(
            "(role = 'user' AND source_run_id IS NULL) OR "
            "(role = 'assistant' AND source_run_id IS NOT NULL)",
            name="message_role_source",
        ),
    )
    op.create_index(
        "uq_conversation_messages_source_run",
        "conversation_messages",
        ["source_run_id"],
        unique=True,
        sqlite_where=text("source_run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("conversation_messages")
    op.drop_table("conversation_run_links")
    op.drop_table("conversation_turns")
    op.drop_table("conversations")
