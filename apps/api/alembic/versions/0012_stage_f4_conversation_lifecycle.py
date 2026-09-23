# ruff: noqa: E501
"""stage f4 conversation lifecycle

Revision ID: 0012_stage_f4_conversation_lifecycle
Revises: 0011_stage_f3_scoped_memory
Create Date: 2026-09-23

F4 implements conversation lifecycle states (active, archived, deleted),
archive and deletion timestamps, and supporting owner-scoped status indexes.
"""

from __future__ import annotations

from alembic import op

revision = "0012_stage_f4_conversation_lifecycle"
down_revision = "0011_stage_f3_scoped_memory"
branch_labels = None
depends_on = None

CONVERSATION_COLUMNS = "id, owner_user_id, agent_instance_id, title, created_at, updated_at"


def conversations_ddl_v2(table: str) -> str:
    return f"""
CREATE TABLE {table} (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    agent_instance_id INTEGER NOT NULL,
    title VARCHAR(400),
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    archived_at DATETIME,
    deleted_at DATETIME,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT fk_conversations_agent_instance_id_agent_instances FOREIGN KEY(agent_instance_id) REFERENCES agent_instances (id) ON DELETE RESTRICT,
    CONSTRAINT fk_conversations_owner_user_id_users FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT ck_conversations_title_shape CHECK (title IS NULL OR (length(title) BETWEEN 1 AND 400 AND instr(title, char(0)) = 0)),
    CONSTRAINT ck_conversations_status_value CHECK (status IN ('active','archived','deleted')),
    CONSTRAINT ck_conversations_lifecycle_timestamps CHECK (((status = 'active' AND archived_at IS NULL AND deleted_at IS NULL) OR (status = 'archived' AND archived_at IS NOT NULL AND deleted_at IS NULL) OR (status = 'deleted' AND deleted_at IS NOT NULL))),
    CONSTRAINT ck_conversations_timestamp_order CHECK (updated_at >= created_at)
)
"""


def conversations_ddl_v1(table: str) -> str:
    return f"""
CREATE TABLE {table} (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL,
    agent_instance_id INTEGER NOT NULL,
    title VARCHAR(400),
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT fk_conversations_agent_instance_id_agent_instances FOREIGN KEY(agent_instance_id) REFERENCES agent_instances (id) ON DELETE RESTRICT,
    CONSTRAINT fk_conversations_owner_user_id_users FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT ck_conversations_title_shape CHECK (title IS NULL OR (length(title) BETWEEN 1 AND 400 AND instr(title, char(0)) = 0)),
    CONSTRAINT ck_conversations_timestamp_order CHECK (updated_at >= created_at)
)
"""


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
        op.execute(conversations_ddl_v2("conversations_new"))
        op.execute(
            f"INSERT INTO conversations_new ({CONVERSATION_COLUMNS}, status, archived_at, deleted_at) "
            f"SELECT {CONVERSATION_COLUMNS}, 'active', NULL, NULL FROM conversations"
        )
        op.execute("DROP TABLE conversations")
        op.execute("ALTER TABLE conversations_new RENAME TO conversations")
        op.execute("CREATE INDEX ix_conversations_owner_id ON conversations (owner_user_id, id)")
        op.execute(
            "CREATE INDEX ix_conversations_owner_agent ON conversations (owner_user_id, agent_instance_id)"
        )
        op.execute(
            "CREATE INDEX ix_conversations_owner_status_id ON conversations (owner_user_id, status, id)"
        )
        op.execute(
            "CREATE INDEX ix_conversations_owner_agent_status_id ON conversations (owner_user_id, agent_instance_id, status, id)"
        )
        op.execute("PRAGMA foreign_keys=ON")
    violations = op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"0012 upgrade preserved no integrity: {violations}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
        op.execute(conversations_ddl_v1("conversations_old"))
        op.execute(
            f"INSERT INTO conversations_old ({CONVERSATION_COLUMNS}) "
            f"SELECT {CONVERSATION_COLUMNS} FROM conversations"
        )
        op.execute("DROP TABLE conversations")
        op.execute("ALTER TABLE conversations_old RENAME TO conversations")
        op.execute("CREATE INDEX ix_conversations_owner_id ON conversations (owner_user_id, id)")
        op.execute(
            "CREATE INDEX ix_conversations_owner_agent ON conversations (owner_user_id, agent_instance_id)"
        )
        op.execute("PRAGMA foreign_keys=ON")
    violations = op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"0012 downgrade preserved no integrity: {violations}")
