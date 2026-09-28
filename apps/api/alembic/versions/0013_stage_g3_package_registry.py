# ruff: noqa: E501
"""stage g3 package registry and immutable package Run snapshots

Revision ID: 0013_stage_g3_package_registry
Revises: 0012_stage_f4_conversation_lifecycle
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from nervos_core.infrastructure.database.types import UTCDateTime

revision = "0013_stage_g3_package_registry"
down_revision = "0012_stage_f4_conversation_lifecycle"
branch_labels = None
depends_on = None

BLANK_TEXT = (
    "char(9,10,11,12,13,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,"
    "8200,8201,8202,8232,8233,8239,8287,12288)"
)
RUN_STATUSES = "'created','running','succeeded','failed','cancelled'"
RUN_LIMITS_POSITIVE = (
    "input_max_bytes > 0 AND input_max_code_points > 0 AND output_max_bytes > 0 AND "
    "output_max_code_points > 0 AND provider_timeout_ms > 0 AND max_output_tokens > 0 AND "
    "max_model_calls > 0"
)
TOOL_LIMITS_BOUNDS = (
    "max_tool_calls BETWEEN 0 AND 16 AND tool_timeout_ms BETWEEN 1000 AND 300000 AND "
    "tool_result_max_bytes BETWEEN 1024 AND 1048576 AND max_consecutive_tool_failures > 0 AND "
    "tool_grant_cutoff_id >= 0"
)
RUN_FINISHED_ORDER = (
    "finished_at IS NULL OR (started_at IS NOT NULL AND finished_at >= started_at) OR "
    "(started_at IS NULL AND finished_at >= created_at AND (error_code = 'worker_recovery_exhausted' "
    "OR status = 'cancelled'))"
)
RUN_LIFECYCLE_SHAPE = (
    "(status='created' AND started_at IS NULL AND finished_at IS NULL AND output_text IS NULL AND "
    "finish_reason IS NULL AND error_code IS NULL AND error_message IS NULL AND input_tokens IS NULL "
    "AND output_tokens IS NULL AND total_tokens IS NULL AND elapsed_ms IS NULL) OR "
    "(status='running' AND started_at IS NOT NULL AND finished_at IS NULL AND output_text IS NULL "
    "AND finish_reason IS NULL AND error_code IS NULL AND error_message IS NULL AND input_tokens IS NULL "
    "AND output_tokens IS NULL AND total_tokens IS NULL AND elapsed_ms IS NULL) OR "
    "(status='succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL AND output_text IS NOT NULL "
    f"AND length(trim(output_text, {BLANK_TEXT})) > 0 AND error_code IS NULL AND error_message IS NULL "
    "AND elapsed_ms IS NOT NULL) OR "
    "(status='failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL AND output_text IS NULL AND "
    "finish_reason IS NULL AND error_code IS NOT NULL AND error_code <> 'worker_recovery_exhausted' AND "
    f"error_message IS NOT NULL AND length(trim(error_message, {BLANK_TEXT})) > 0 AND elapsed_ms IS NOT NULL) OR "
    "(status='failed' AND started_at IS NULL AND finished_at IS NOT NULL AND output_text IS NULL AND "
    "finish_reason IS NULL AND error_code = 'worker_recovery_exhausted' AND error_message IS NOT NULL AND "
    f"length(trim(error_message, {BLANK_TEXT})) > 0 AND input_tokens IS NULL AND output_tokens IS NULL AND "
    "total_tokens IS NULL AND elapsed_ms IS NULL) OR "
    "(status='cancelled' AND started_at IS NOT NULL AND finished_at IS NOT NULL AND output_text IS NULL AND "
    "finish_reason IS NULL AND error_code IS NULL AND error_message IS NULL AND input_tokens IS NULL AND "
    "output_tokens IS NULL AND total_tokens IS NULL AND elapsed_ms IS NOT NULL) OR "
    "(status='cancelled' AND started_at IS NULL AND finished_at IS NOT NULL AND output_text IS NULL AND "
    "finish_reason IS NULL AND error_code IS NULL AND error_message IS NULL AND input_tokens IS NULL AND "
    "output_tokens IS NULL AND total_tokens IS NULL AND elapsed_ms IS NULL)"
)
EXECUTION_SNAPSHOT_SHAPE = (
    "(execution_kind = 'builtin' AND installed_package_version_id IS NULL AND "
    "package_content_digest IS NULL AND package_environment_id IS NULL AND "
    "package_environment_digest IS NULL AND package_entrypoint IS NULL AND "
    "effective_config_json = '{}' AND effective_config_digest IS NULL AND "
    "agent_instance_config_revision IS NULL AND host_protocol_version IS NULL AND "
    "sdk_api_version IS NULL) OR "
    "(execution_kind = 'package' AND installed_package_version_id IS NOT NULL AND "
    "package_content_digest IS NOT NULL AND package_environment_id IS NOT NULL AND "
    "package_environment_digest IS NOT NULL AND package_entrypoint IS NOT NULL AND "
    "effective_config_digest IS NOT NULL AND agent_instance_config_revision IS NOT NULL AND "
    "host_protocol_version IS NOT NULL AND sdk_api_version IS NOT NULL)"
)

SHARED_RUN_COLUMNS = (
    "id, agent_instance_id, status, agent_key, agent_definition_version, model_provider, "
    "model_name, input_text, input_max_bytes, input_max_code_points, output_max_bytes, "
    "output_max_code_points, provider_timeout_ms, max_output_tokens, max_model_calls, "
    "max_tool_calls, tool_timeout_ms, tool_result_max_bytes, max_consecutive_tool_failures, "
    "tool_grant_cutoff_id, output_text, finish_reason, error_code, error_message, "
    "input_tokens, output_tokens, total_tokens, elapsed_ms, created_at, started_at, finished_at"
)


def runs_ddl_v2(table: str) -> str:
    return f"""
CREATE TABLE {table} (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    agent_instance_id INTEGER NOT NULL,
    status VARCHAR(16) NOT NULL,
    agent_key VARCHAR(128) NOT NULL,
    agent_definition_version VARCHAR(64) NOT NULL,
    model_provider VARCHAR(64) NOT NULL,
    model_name VARCHAR(256) NOT NULL,
    input_text TEXT NOT NULL,
    input_max_bytes INTEGER NOT NULL,
    input_max_code_points INTEGER NOT NULL,
    output_max_bytes INTEGER NOT NULL,
    output_max_code_points INTEGER NOT NULL,
    provider_timeout_ms INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    max_model_calls INTEGER NOT NULL,
    max_tool_calls INTEGER NOT NULL DEFAULT '0',
    tool_timeout_ms INTEGER NOT NULL DEFAULT '30000',
    tool_result_max_bytes INTEGER NOT NULL DEFAULT '65536',
    max_consecutive_tool_failures INTEGER NOT NULL DEFAULT '3',
    tool_grant_cutoff_id INTEGER NOT NULL DEFAULT '0',
    execution_kind VARCHAR(16) NOT NULL DEFAULT 'builtin',
    installed_package_version_id INTEGER,
    package_content_digest VARCHAR(64),
    package_environment_id INTEGER,
    package_environment_digest VARCHAR(64),
    package_entrypoint VARCHAR(512),
    effective_config_json TEXT NOT NULL DEFAULT '{{}}',
    effective_config_digest VARCHAR(64),
    agent_instance_config_revision INTEGER,
    host_protocol_version VARCHAR(16),
    sdk_api_version VARCHAR(16),
    output_text TEXT,
    finish_reason VARCHAR(64),
    error_code VARCHAR(64),
    error_message VARCHAR(512),
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    elapsed_ms INTEGER,
    created_at DATETIME NOT NULL,
    started_at DATETIME,
    finished_at DATETIME,
    CONSTRAINT ck_runs_agent_instance_positive CHECK (agent_instance_id > 0),
    CONSTRAINT ck_runs_status_value CHECK (status IN ({RUN_STATUSES})),
    CONSTRAINT ck_runs_limits_positive CHECK ({RUN_LIMITS_POSITIVE}),
    CONSTRAINT ck_runs_tool_limits_bounds CHECK ({TOOL_LIMITS_BOUNDS}),
    CONSTRAINT ck_runs_input_bounds CHECK (length(input_text) BETWEEN 1 AND input_max_code_points AND length(CAST(input_text AS BLOB)) <= input_max_bytes AND instr(input_text, char(0)) = 0 AND length(trim(input_text, {BLANK_TEXT})) > 0),
    CONSTRAINT ck_runs_output_bounds CHECK (output_text IS NULL OR (length(output_text) BETWEEN 1 AND output_max_code_points AND length(CAST(output_text AS BLOB)) <= output_max_bytes AND instr(output_text, char(0)) = 0)),
    CONSTRAINT ck_runs_usage_nonnegative CHECK ((input_tokens IS NULL OR input_tokens >= 0) AND (output_tokens IS NULL OR output_tokens >= 0) AND (total_tokens IS NULL OR total_tokens >= 0)),
    CONSTRAINT ck_runs_elapsed_nonnegative CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    CONSTRAINT ck_runs_started_order CHECK (started_at IS NULL OR started_at >= created_at),
    CONSTRAINT ck_runs_finished_order CHECK ({RUN_FINISHED_ORDER}),
    CONSTRAINT ck_runs_lifecycle_shape CHECK ({RUN_LIFECYCLE_SHAPE}),
    CONSTRAINT ck_runs_execution_kind_value CHECK (execution_kind IN ('builtin','package')),
    CONSTRAINT ck_runs_execution_snapshot_shape CHECK ({EXECUTION_SNAPSHOT_SHAPE}),
    CONSTRAINT fk_runs_agent_instance_id_agent_instances FOREIGN KEY(agent_instance_id) REFERENCES agent_instances (id) ON DELETE RESTRICT,
    CONSTRAINT fk_runs_installed_package_version_id_installed_package_versions FOREIGN KEY(installed_package_version_id) REFERENCES installed_package_versions (id) ON DELETE RESTRICT,
    CONSTRAINT fk_runs_package_environment_id_package_environments FOREIGN KEY(package_environment_id) REFERENCES package_environments (id) ON DELETE RESTRICT
)
"""


def runs_ddl_v1(table: str) -> str:
    return f"""
CREATE TABLE {table} (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    agent_instance_id INTEGER NOT NULL,
    status VARCHAR(16) NOT NULL,
    agent_key VARCHAR(128) NOT NULL,
    agent_definition_version VARCHAR(64) NOT NULL,
    model_provider VARCHAR(64) NOT NULL,
    model_name VARCHAR(256) NOT NULL,
    input_text TEXT NOT NULL,
    input_max_bytes INTEGER NOT NULL,
    input_max_code_points INTEGER NOT NULL,
    output_max_bytes INTEGER NOT NULL,
    output_max_code_points INTEGER NOT NULL,
    provider_timeout_ms INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    max_model_calls INTEGER NOT NULL,
    max_tool_calls INTEGER NOT NULL DEFAULT '0',
    tool_timeout_ms INTEGER NOT NULL DEFAULT '30000',
    tool_result_max_bytes INTEGER NOT NULL DEFAULT '65536',
    max_consecutive_tool_failures INTEGER NOT NULL DEFAULT '3',
    tool_grant_cutoff_id INTEGER NOT NULL DEFAULT '0',
    output_text TEXT,
    finish_reason VARCHAR(64),
    error_code VARCHAR(64),
    error_message VARCHAR(512),
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    elapsed_ms INTEGER,
    created_at DATETIME NOT NULL,
    started_at DATETIME,
    finished_at DATETIME,
    CONSTRAINT ck_runs_agent_instance_positive CHECK (agent_instance_id > 0),
    CONSTRAINT ck_runs_status_value CHECK (status IN ({RUN_STATUSES})),
    CONSTRAINT ck_runs_limits_positive CHECK ({RUN_LIMITS_POSITIVE}),
    CONSTRAINT ck_runs_tool_limits_bounds CHECK ({TOOL_LIMITS_BOUNDS}),
    CONSTRAINT ck_runs_input_bounds CHECK (length(input_text) BETWEEN 1 AND input_max_code_points AND length(CAST(input_text AS BLOB)) <= input_max_bytes AND instr(input_text, char(0)) = 0 AND length(trim(input_text, {BLANK_TEXT})) > 0),
    CONSTRAINT ck_runs_output_bounds CHECK (output_text IS NULL OR (length(output_text) BETWEEN 1 AND output_max_code_points AND length(CAST(output_text AS BLOB)) <= output_max_bytes AND instr(output_text, char(0)) = 0)),
    CONSTRAINT ck_runs_usage_nonnegative CHECK ((input_tokens IS NULL OR input_tokens >= 0) AND (output_tokens IS NULL OR output_tokens >= 0) AND (total_tokens IS NULL OR total_tokens >= 0)),
    CONSTRAINT ck_runs_elapsed_nonnegative CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    CONSTRAINT ck_runs_started_order CHECK (started_at IS NULL OR started_at >= created_at),
    CONSTRAINT ck_runs_finished_order CHECK ({RUN_FINISHED_ORDER}),
    CONSTRAINT ck_runs_lifecycle_shape CHECK ({RUN_LIFECYCLE_SHAPE}),
    CONSTRAINT fk_runs_agent_instance_id_agent_instances FOREIGN KEY(agent_instance_id) REFERENCES agent_instances (id) ON DELETE RESTRICT
)
"""


def upgrade() -> None:
    op.create_table(
        "package_environments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("environment_digest", sa.String(64), nullable=False),
        sa.Column("environment_key_json", sa.Text(), nullable=False),
        sa.Column("environment_key", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("python_version", sa.String(32), nullable=False),
        sa.Column("sdk_version", sa.String(64), nullable=False),
        sa.Column("sdk_wheel_digest", sa.String(64), nullable=False),
        sa.Column("host_version", sa.String(64), nullable=False),
        sa.Column("host_wheel_digest", sa.String(64), nullable=False),
        sa.Column("host_protocol_version", sa.String(16), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("ready_at", UTCDateTime(), nullable=True),
        sa.Column("failed_at", UTCDateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("last_error_message", sa.String(512), nullable=True),
        sa.CheckConstraint(
            "length(environment_digest) = 64 AND environment_digest NOT GLOB '*[^0-9a-f]*'",
            name="environment_digest_hex",
        ),
        sa.CheckConstraint(
            "length(sdk_wheel_digest) = 64 AND sdk_wheel_digest NOT GLOB '*[^0-9a-f]*'",
            name="sdk_wheel_digest_hex",
        ),
        sa.CheckConstraint(
            "length(host_wheel_digest) = 64 AND host_wheel_digest NOT GLOB '*[^0-9a-f]*'",
            name="host_wheel_digest_hex",
        ),
        sa.CheckConstraint("status IN ('preparing','ready','failed')", name="status_value"),
        sa.CheckConstraint("python_version = '3.12'", name="python_version_value"),
        sa.CheckConstraint("host_protocol_version = '1'", name="host_protocol_version_value"),
        sa.CheckConstraint(
            "(last_error_code IS NULL AND last_error_message IS NULL) OR (last_error_code IS NOT NULL AND last_error_message IS NOT NULL)",
            name="error_pair",
        ),
        sa.CheckConstraint("updated_at >= created_at", name="timestamp_order"),
        sa.UniqueConstraint("environment_digest"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "installed_package_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("package_id", sa.String(128), nullable=False),
        sa.Column("package_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("archive_digest", sa.String(64), nullable=False),
        sa.Column("signer_public_key", sa.LargeBinary(32), nullable=False),
        sa.Column("signer_fingerprint", sa.String(64), nullable=False),
        sa.Column("manifest_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("config_schema_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("dependency_lock_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("agent_wheel_name", sa.String(256), nullable=False),
        sa.Column("agent_wheel_version", sa.String(128), nullable=False),
        sa.Column("agent_wheel_sha256", sa.String(64), nullable=False),
        sa.Column("agent_wheel_size", sa.Integer(), nullable=False),
        sa.Column("entrypoint_module", sa.String(256), nullable=False),
        sa.Column("entrypoint_object", sa.String(256), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=True),
        sa.Column("staging_key", sa.String(512), nullable=True),
        sa.Column("environment_id", sa.Integer(), nullable=True),
        sa.Column("approved_by_user_id", sa.Integer(), nullable=True),
        sa.Column("approved_at", UTCDateTime(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("installed_at", UTCDateTime(), nullable=True),
        sa.Column("activated_at", UTCDateTime(), nullable=True),
        sa.Column("failed_at", UTCDateTime(), nullable=True),
        sa.Column("removed_at", UTCDateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("last_error_message", sa.String(512), nullable=True),
        sa.CheckConstraint("package_id NOT LIKE 'nervos.%'", name="package_id_not_reserved"),
        sa.CheckConstraint(
            "status IN ('installing','installed','active','failed','pending_removal','removed')",
            name="status_value",
        ),
        sa.CheckConstraint(
            "length(content_digest) = 64 AND content_digest NOT GLOB '*[^0-9a-f]*'",
            name="content_digest_hex",
        ),
        sa.CheckConstraint(
            "length(archive_digest) = 64 AND archive_digest NOT GLOB '*[^0-9a-f]*'",
            name="archive_digest_hex",
        ),
        sa.CheckConstraint(
            "length(signer_fingerprint) = 64 AND signer_fingerprint NOT GLOB '*[^0-9a-f]*'",
            name="signer_fingerprint_hex",
        ),
        sa.CheckConstraint("length(signer_public_key) = 32", name="signer_public_key_size"),
        sa.CheckConstraint("agent_wheel_size > 0", name="agent_wheel_size_positive"),
        sa.CheckConstraint(
            "(last_error_code IS NULL AND last_error_message IS NULL) OR (last_error_code IS NOT NULL AND last_error_message IS NOT NULL)",
            name="error_pair",
        ),
        sa.CheckConstraint("updated_at >= created_at", name="timestamp_order"),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["package_environments.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("package_id", "package_version"),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_installed_package_versions_status_id", "installed_package_versions", ["status", "id"]
    )
    op.create_index(
        "ix_installed_package_versions_content_digest",
        "installed_package_versions",
        ["content_digest"],
    )
    op.create_table(
        "installed_package_files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("installed_package_version_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'", name="sha256_hex"
        ),
        sa.CheckConstraint("byte_length >= 0", name="byte_length_nonnegative"),
        sa.ForeignKeyConstraint(
            ["installed_package_version_id"], ["installed_package_versions.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("installed_package_version_id", "path"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "installed_package_dependencies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("installed_package_version_id", sa.Integer(), nullable=False),
        sa.Column("distribution_name", sa.String(256), nullable=False),
        sa.Column("distribution_version", sa.String(128), nullable=False),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'", name="sha256_hex"
        ),
        sa.CheckConstraint("byte_length > 0", name="byte_length_positive"),
        sa.ForeignKeyConstraint(
            ["installed_package_version_id"], ["installed_package_versions.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("installed_package_version_id", "distribution_name"),
        sa.UniqueConstraint("installed_package_version_id", "filename"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "agent_instance_package_bindings",
        sa.Column("agent_instance_id", sa.Integer(), primary_key=True),
        sa.Column("installed_package_version_id", sa.Integer(), nullable=False),
        sa.Column("effective_config_json", sa.Text(), nullable=False),
        sa.Column("effective_config_digest", sa.String(64), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("config_schema_digest", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "length(effective_config_digest) = 64 AND effective_config_digest NOT GLOB '*[^0-9a-f]*'",
            name="effective_config_digest_hex",
        ),
        sa.CheckConstraint(
            "length(config_schema_digest) = 64 AND config_schema_digest NOT GLOB '*[^0-9a-f]*'",
            name="config_schema_digest_hex",
        ),
        sa.CheckConstraint("config_revision > 0", name="config_revision_positive"),
        sa.CheckConstraint("updated_at >= created_at", name="timestamp_order"),
        sa.ForeignKeyConstraint(["agent_instance_id"], ["agent_instances.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["installed_package_version_id"], ["installed_package_versions.id"], ondelete="RESTRICT"
        ),
    )

    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
        op.execute(runs_ddl_v2("runs_new"))
        op.execute(
            f"INSERT INTO runs_new ({SHARED_RUN_COLUMNS}, execution_kind, effective_config_json) "
            f"SELECT {SHARED_RUN_COLUMNS}, 'builtin', '{{}}' FROM runs"
        )
        op.execute("DROP TABLE runs")
        op.execute("ALTER TABLE runs_new RENAME TO runs")
        op.execute("CREATE INDEX ix_runs_agent_instance_id_id ON runs (agent_instance_id, id)")
        op.execute(
            "CREATE INDEX ix_runs_installed_package_version_id ON runs (installed_package_version_id)"
        )
        op.execute("CREATE INDEX ix_runs_package_environment_id ON runs (package_environment_id)")
        op.execute("PRAGMA foreign_keys=ON")

    violations = op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"0013 upgrade preserved no integrity: {violations}")


def downgrade() -> None:
    connection = op.get_bind()
    package_runs = connection.exec_driver_sql(
        "SELECT count(*) FROM runs WHERE execution_kind = 'package'"
    ).scalar_one()
    if package_runs:
        raise RuntimeError(
            "0013 downgrade refused: package-backed Runs would lose executable evidence"
        )

    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
        op.execute(runs_ddl_v1("runs_old"))
        op.execute(
            f"INSERT INTO runs_old ({SHARED_RUN_COLUMNS}) SELECT {SHARED_RUN_COLUMNS} FROM runs"
        )
        op.execute("DROP TABLE runs")
        op.execute("ALTER TABLE runs_old RENAME TO runs")
        op.execute("CREATE INDEX ix_runs_agent_instance_id_id ON runs (agent_instance_id, id)")
        op.execute("PRAGMA foreign_keys=ON")

    violations = op.get_bind().exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"0013 downgrade preserved no integrity: {violations}")

    op.drop_table("agent_instance_package_bindings")
    op.drop_table("installed_package_dependencies")
    op.drop_table("installed_package_files")
    op.drop_index(
        "ix_installed_package_versions_content_digest", table_name="installed_package_versions"
    )
    op.drop_index(
        "ix_installed_package_versions_status_id", table_name="installed_package_versions"
    )
    op.drop_table("installed_package_versions")
    op.drop_table("package_environments")
