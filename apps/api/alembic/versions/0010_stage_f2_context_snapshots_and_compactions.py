# ruff: noqa: E501
"""stage f2 context snapshots, compactions, and legacy context mode

Revision ID: 0010_stage_f2_context_snapshots_and_compactions
Revises: 0009_stage_f1_conversations
Create Date: 2026-09-22

F2 implements provider-neutral multi-turn ContextBuilder context snapshots,
deterministic non-model conversation compactions, and legacy F1 context mode markers.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "0010_stage_f2_context_snapshots_and_compactions"
down_revision = "0009_stage_f1_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Recreate conversation_run_links with context_mode column and check constraint
    op.execute(
        """
        CREATE TABLE conversation_run_links_new (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            turn_id INTEGER NOT NULL,
            run_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            role VARCHAR(16) NOT NULL,
            context_mode VARCHAR(32) NOT NULL DEFAULT 'f1_single_turn',
            created_at DATETIME NOT NULL,
            CONSTRAINT ck_conversation_run_links_ordinal_positive CHECK (ordinal >= 1),
            CONSTRAINT ck_conversation_run_links_link_role_value CHECK (role IN ('initial','retry')),
            CONSTRAINT ck_conversation_run_links_context_mode_value CHECK (context_mode IN ('f1_single_turn','f2_context_snapshot')),
            CONSTRAINT fk_conversation_run_links_run_id_runs FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE RESTRICT,
            CONSTRAINT fk_conversation_run_links_turn_id_conversation_turns FOREIGN KEY(turn_id) REFERENCES conversation_turns (id) ON DELETE RESTRICT,
            CONSTRAINT uq_conversation_run_links_run UNIQUE (run_id),
            CONSTRAINT uq_conversation_run_links_turn_ordinal UNIQUE (turn_id, ordinal),
            CONSTRAINT uq_conversation_run_links_turn_run UNIQUE (turn_id, run_id)
        );
        """
    )
    op.execute(
        """
        INSERT INTO conversation_run_links_new (id, turn_id, run_id, ordinal, role, context_mode, created_at)
        SELECT id, turn_id, run_id, ordinal, role, 'f1_single_turn', created_at FROM conversation_run_links;
        """
    )
    op.execute("DROP TABLE conversation_run_links;")
    op.execute("ALTER TABLE conversation_run_links_new RENAME TO conversation_run_links;")

    # 2. run_context_snapshots
    op.create_table(
        "run_context_snapshots",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("turn_id", sa.Integer(), nullable=False),
        sa.Column(
            "schema_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("builder_version", sa.String(length=64), nullable=False),
        sa.Column("current_user_text", sa.Text(), nullable=False),
        sa.Column("history_messages", sa.Text(), nullable=False),
        sa.Column("selected_turn_ids", sa.Text(), nullable=False),
        sa.Column("selected_message_ids", sa.Text(), nullable=False),
        sa.Column("compaction_version", sa.Integer(), nullable=True),
        sa.Column("compaction_source_start", sa.Integer(), nullable=True),
        sa.Column("compaction_source_end", sa.Integer(), nullable=True),
        sa.Column("injected_compaction_text", sa.Text(), nullable=True),
        sa.Column("agent_key", sa.String(length=128), nullable=False),
        sa.Column("agent_definition_version", sa.String(length=64), nullable=False),
        sa.Column(
            "max_total_bytes",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("8000"),
        ),
        sa.Column(
            "max_total_code_points",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("4000"),
        ),
        sa.Column("actual_total_bytes", sa.Integer(), nullable=False),
        sa.Column("actual_total_code_points", sa.Integer(), nullable=False),
        sa.Column("rendered_context", sa.Text(), nullable=False),
        sa.Column("content_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["turn_id"], ["conversation_turns.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["turn_id", "run_id"],
            ["conversation_run_links.turn_id", "conversation_run_links.run_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.CheckConstraint("schema_version > 0", name="schema_version_positive"),
        sa.CheckConstraint(
            "length(builder_version) BETWEEN 1 AND 64", name="builder_version_length"
        ),
        sa.CheckConstraint(
            "max_total_bytes > 0 AND max_total_code_points > 0", name="max_bounds_positive"
        ),
        sa.CheckConstraint(
            "actual_total_bytes >= 0 AND actual_total_code_points >= 0",
            name="actual_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "actual_total_bytes <= max_total_bytes AND actual_total_code_points <= max_total_code_points",
            name="actual_within_bounds",
        ),
        sa.CheckConstraint(
            "compaction_version IS NULL OR (compaction_source_start IS NOT NULL AND compaction_source_end IS NOT NULL AND compaction_source_start <= compaction_source_end AND compaction_source_start > 0)",
            name="compaction_provenance_valid",
        ),
        sa.CheckConstraint("length(content_digest) = 32", name="content_digest_length"),
    )
    op.create_index(
        "ix_run_context_snapshots_turn_id",
        "run_context_snapshots",
        ["turn_id"],
        unique=False,
    )

    # 3. conversation_compactions
    op.create_table(
        "conversation_compactions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_start_sequence", sa.Integer(), nullable=False),
        sa.Column("source_end_sequence", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "is_current",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id", "version", name="uq_conversation_compactions_version"
        ),
        sa.CheckConstraint("version > 0", name="compaction_version_positive"),
        sa.CheckConstraint(
            "source_start_sequence > 0 AND source_end_sequence >= source_start_sequence",
            name="compaction_sequence_range",
        ),
        sa.CheckConstraint(
            "length(content) BETWEEN 1 AND 32000 AND length(CAST(content AS BLOB)) <= 32000",
            name="compaction_content_bounds",
        ),
        sa.CheckConstraint("length(content_digest) = 32", name="compaction_digest_length"),
    )
    op.create_index(
        "uq_conversation_compactions_one_current",
        "conversation_compactions",
        ["conversation_id"],
        unique=True,
        sqlite_where=text("is_current = 1"),
    )


def downgrade() -> None:
    op.drop_table("conversation_compactions")
    op.drop_table("run_context_snapshots")
    op.execute(
        """
        CREATE TABLE conversation_run_links_old (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            turn_id INTEGER NOT NULL,
            run_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            role VARCHAR(16) NOT NULL,
            created_at DATETIME NOT NULL,
            CONSTRAINT ck_conversation_run_links_ordinal_positive CHECK (ordinal >= 1),
            CONSTRAINT ck_conversation_run_links_link_role_value CHECK (role IN ('initial','retry')),
            CONSTRAINT fk_conversation_run_links_run_id_runs FOREIGN KEY(run_id) REFERENCES runs (id) ON DELETE RESTRICT,
            CONSTRAINT fk_conversation_run_links_turn_id_conversation_turns FOREIGN KEY(turn_id) REFERENCES conversation_turns (id) ON DELETE RESTRICT,
            CONSTRAINT uq_conversation_run_links_run UNIQUE (run_id),
            CONSTRAINT uq_conversation_run_links_turn_ordinal UNIQUE (turn_id, ordinal),
            CONSTRAINT uq_conversation_run_links_turn_run UNIQUE (turn_id, run_id)
        );
        """
    )
    op.execute(
        """
        INSERT INTO conversation_run_links_old (id, turn_id, run_id, ordinal, role, created_at)
        SELECT id, turn_id, run_id, ordinal, role, created_at FROM conversation_run_links;
        """
    )
    op.execute("DROP TABLE conversation_run_links;")
    op.execute("ALTER TABLE conversation_run_links_old RENAME TO conversation_run_links;")
