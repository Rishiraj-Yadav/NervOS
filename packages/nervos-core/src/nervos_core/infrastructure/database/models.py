# ruff: noqa: E501
"""SQLAlchemy persistence records for the current application schema."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from nervos_core.infrastructure.database.base import Base
from nervos_core.infrastructure.database.types import UTCDateTime

NERVOS_BLANK_TEXT_SQL_CHARS = (
    "char(9,10,11,12,13,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,"
    "8200,8201,8202,8232,8233,8239,8287,12288)"
)

# Stage D (D1) vocabulary. The tool event types are appended to the C1 vocabulary rather than
# interleaved with it, so the thirteen accepted Stage C event types remain a byte-for-byte prefix
# of the accepted set.
TOOL_EVENT_TYPES = (
    "'tool.requested','tool.started','tool.succeeded','tool.failed','tool.denied','tool.ambiguous'"
    ",'tool.approval_requested','tool.approval_decided'"
)
CONNECTION_STATUSES = "'connected','unavailable','needs_refresh','definition_changed','disabled'"
DEFINITION_STATUSES = "'available','unavailable','unsupported_schema'"
INVOCATION_STATUSES = "'requested','denied','cancelled','started','succeeded','failed','ambiguous'"
INVOCATION_DISPATCHED_STATUSES = "'started','succeeded','failed','ambiguous'"
PACKAGE_INSTALL_STATUSES = "'installing','installed','active','failed','pending_removal','removed'"
PACKAGE_ENVIRONMENT_STATUSES = "'preparing','ready','failed'"
EXECUTION_KINDS = "'builtin','package'"
SHA256_SQL = "length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"
# The intersection of both providers' tool-name alphabets and the stricter of their two length
# bounds, so one persisted name is valid unchanged for either provider.
MODEL_NAME_SQL = (
    "model_name = lower(model_name) AND length(model_name) BETWEEN 1 AND 64"
    " AND model_name NOT GLOB '*[^a-z0-9_-]*' AND model_name GLOB '[a-z0-9]*'"
)


class AgentMemoryPolicyRecord(Base):
    __tablename__ = "agent_memory_policies"
    __table_args__ = (
        CheckConstraint("mode IN ('manual','review','automatic_private')", name="mode_value"),
        CheckConstraint("revision > 0", name="revision_positive"),
    )
    agent_instance_id: Mapped[int] = mapped_column(
        ForeignKey("agent_instances.id"), primary_key=True
    )
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AgentToolBindingRecord(Base):
    __tablename__ = "agent_tool_bindings"
    __table_args__ = (
        UniqueConstraint("agent_instance_id", "alias", name="instance_alias"),
        CheckConstraint("length(alias) BETWEEN 1 AND 128", name="alias_bound"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_instance_id: Mapped[int] = mapped_column(ForeignKey("agent_instances.id"), nullable=False)
    alias: Mapped[str] = mapped_column(String(128), nullable=False)
    package_id: Mapped[str] = mapped_column(String(128), nullable=False)
    package_version: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_definition_id: Mapped[int] = mapped_column(
        ForeignKey("tool_definitions.id"), nullable=False
    )
    reviewed_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RunIntegrationRecord(Base):
    __tablename__ = "run_integrations"
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    context_json: Mapped[str | None] = mapped_column(Text)
    bindings_json: Mapped[str] = mapped_column(Text, nullable=False)
    memory_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    proposals_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    memory_state: Mapped[str] = mapped_column(String(24), nullable=False, default="none")
    extraction_run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"))


class MemorySuggestionRecord(Base):
    __tablename__ = "memory_suggestions"
    __table_args__ = (
        UniqueConstraint("source_run_id", "scope", "content_digest", name="source_fact"),
        CheckConstraint("scope IN ('user','agent')", name="scope_value"),
        CheckConstraint(
            "state IN ('pending','saved','dismissed','duplicate','quota')", name="state_value"
        ),
        CheckConstraint("length(CAST(content AS BLOB)) BETWEEN 1 AND 2000", name="content_bound"),
        Index("ix_memory_suggestions_owner_id", "owner_user_id", "id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    agent_instance_id: Mapped[int] = mapped_column(ForeignKey("agent_instances.id"), nullable=False)
    source_run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    scope: Mapped[str] = mapped_column(String(8), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    memory_item_id: Mapped[int | None] = mapped_column(ForeignKey("memory_items.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class MarketplaceInstallRequestRecord(Base):
    """Owner-scoped exact download evidence; Stage G owns installed packages."""

    __tablename__ = "marketplace_install_requests"
    __table_args__ = (
        CheckConstraint(
            "state IN ('created','downloaded','approved','installed','failed')", name="state_value"
        ),
        CheckConstraint("length(package_id) BETWEEN 1 AND 128", name="package_id_bound"),
        CheckConstraint("length(package_version) BETWEEN 1 AND 64", name="version_bound"),
        CheckConstraint("observed_status_revision > 0", name="revision_positive"),
        CheckConstraint("expected_size_bytes >= 0", name="size_nonnegative"),
        Index("ix_marketplace_install_requests_owner", "owner_user_id", "created_at"),
        ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="owner_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    marketplace_origin: Mapped[str] = mapped_column(String(512), nullable=False)
    package_id: Mapped[str] = mapped_column(String(128), nullable=False)
    package_version: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_signer_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_status_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_distribution_state: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="created")
    artifact_path: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class UserRecord(Base):
    """Persistence record for a NervOS user."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username"),
        CheckConstraint("username = lower(trim(username))", name="username_canonical"),
        CheckConstraint("length(username) BETWEEN 3 AND 32", name="username_length"),
        CheckConstraint("length(trim(role)) > 0", name="role_nonempty"),
        CheckConstraint("updated_at >= created_at", name="timestamp_order"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AgentInstanceRecord(Base):
    """Persistence record for explicit user-owned Agent configuration."""

    __tablename__ = "agent_instances"
    __table_args__ = (
        CheckConstraint("owner_user_id > 0", name="owner_positive"),
        CheckConstraint("length(agent_key) BETWEEN 1 AND 128", name="agent_key_length"),
        CheckConstraint(
            "length(agent_definition_version) BETWEEN 1 AND 64", name="definition_version_length"
        ),
        CheckConstraint(
            "display_name = trim(display_name) AND length(display_name) BETWEEN 1 AND 100",
            name="display_name_shape",
        ),
        CheckConstraint("length(CAST(display_name AS BLOB)) <= 400", name="display_name_bytes"),
        CheckConstraint(
            "length(model_provider) BETWEEN 1 AND 64 AND model_provider = lower(model_provider)",
            name="provider_shape",
        ),
        CheckConstraint(
            "model_name = trim(model_name) AND length(model_name) BETWEEN 1 AND 256",
            name="model_name_shape",
        ),
        CheckConstraint("length(CAST(model_name AS BLOB)) <= 1024", name="model_name_bytes"),
        CheckConstraint("enabled IN (0, 1)", name="enabled_boolean"),
        CheckConstraint("updated_at >= created_at", name="timestamp_order"),
        Index("ix_agent_instances_owner_user_id_id", "owner_user_id", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    agent_key: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_definition_version: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    model_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class PackageEnvironmentRecord(Base):
    """Content-addressed isolated Python environment for installed packages."""

    __tablename__ = "package_environments"
    __table_args__ = (
        UniqueConstraint("environment_digest"),
        CheckConstraint(
            SHA256_SQL.format(column="environment_digest"), name="environment_digest_hex"
        ),
        CheckConstraint(SHA256_SQL.format(column="sdk_wheel_digest"), name="sdk_wheel_digest_hex"),
        CheckConstraint(
            SHA256_SQL.format(column="host_wheel_digest"), name="host_wheel_digest_hex"
        ),
        CheckConstraint(f"status IN ({PACKAGE_ENVIRONMENT_STATUSES})", name="status_value"),
        CheckConstraint("python_version = '3.12'", name="python_version_value"),
        CheckConstraint("host_protocol_version = '1'", name="host_protocol_version_value"),
        CheckConstraint(
            "(last_error_code IS NULL AND last_error_message IS NULL) OR "
            "(last_error_code IS NOT NULL AND last_error_message IS NOT NULL)",
            name="error_pair",
        ),
        CheckConstraint("updated_at >= created_at", name="timestamp_order"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    environment_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    environment_key_json: Mapped[str] = mapped_column(Text, nullable=False)
    environment_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    python_version: Mapped[str] = mapped_column(String(32), nullable=False)
    sdk_version: Mapped[str] = mapped_column(String(64), nullable=False)
    sdk_wheel_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    host_version: Mapped[str] = mapped_column(String(64), nullable=False)
    host_wheel_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    host_protocol_version: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    ready_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)


class InstalledPackageVersionRecord(Base):
    """One node-global exact installed package release."""

    __tablename__ = "installed_package_versions"
    __table_args__ = (
        UniqueConstraint("package_id", "package_version"),
        CheckConstraint("package_id NOT LIKE 'nervos.%'", name="package_id_not_reserved"),
        CheckConstraint(f"status IN ({PACKAGE_INSTALL_STATUSES})", name="status_value"),
        CheckConstraint(SHA256_SQL.format(column="content_digest"), name="content_digest_hex"),
        CheckConstraint(SHA256_SQL.format(column="archive_digest"), name="archive_digest_hex"),
        CheckConstraint(
            SHA256_SQL.format(column="signer_fingerprint"), name="signer_fingerprint_hex"
        ),
        CheckConstraint("length(signer_public_key) = 32", name="signer_public_key_size"),
        CheckConstraint("agent_wheel_size > 0", name="agent_wheel_size_positive"),
        CheckConstraint(
            "(last_error_code IS NULL AND last_error_message IS NULL) OR "
            "(last_error_code IS NOT NULL AND last_error_message IS NOT NULL)",
            name="error_pair",
        ),
        CheckConstraint("updated_at >= created_at", name="timestamp_order"),
        Index("ix_installed_package_versions_status_id", "status", "id"),
        Index("ix_installed_package_versions_content_digest", "content_digest"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    package_id: Mapped[str] = mapped_column(String(128), nullable=False)
    package_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    signer_public_key: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    signer_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    config_schema_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dependency_lock_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    agent_wheel_name: Mapped[str] = mapped_column(String(256), nullable=False)
    agent_wheel_version: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_wheel_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_wheel_size: Mapped[int] = mapped_column(Integer, nullable=False)
    entrypoint_module: Mapped[str] = mapped_column(String(256), nullable=False)
    entrypoint_object: Mapped[str] = mapped_column(String(256), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    staging_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    environment_id: Mapped[int | None] = mapped_column(
        ForeignKey("package_environments.id", ondelete="RESTRICT"), nullable=True
    )
    approved_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    installed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)


class InstalledPackageFileRecord(Base):
    """One G2-verified payload file belonging to an installed package version."""

    __tablename__ = "installed_package_files"
    __table_args__ = (
        UniqueConstraint("installed_package_version_id", "path"),
        CheckConstraint(SHA256_SQL.format(column="sha256"), name="sha256_hex"),
        CheckConstraint("byte_length >= 0", name="byte_length_nonnegative"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    installed_package_version_id: Mapped[int] = mapped_column(
        ForeignKey("installed_package_versions.id", ondelete="RESTRICT"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_length: Mapped[int] = mapped_column(Integer, nullable=False)


class InstalledPackageDependencyRecord(Base):
    """One verified dependency wheel belonging to an installed package version."""

    __tablename__ = "installed_package_dependencies"
    __table_args__ = (
        UniqueConstraint("installed_package_version_id", "distribution_name"),
        UniqueConstraint("installed_package_version_id", "filename"),
        CheckConstraint(SHA256_SQL.format(column="sha256"), name="sha256_hex"),
        CheckConstraint("byte_length > 0", name="byte_length_positive"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    installed_package_version_id: Mapped[int] = mapped_column(
        ForeignKey("installed_package_versions.id", ondelete="RESTRICT"), nullable=False
    )
    distribution_name: Mapped[str] = mapped_column(String(256), nullable=False)
    distribution_version: Mapped[str] = mapped_column(String(128), nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_length: Mapped[int] = mapped_column(Integer, nullable=False)


class AgentInstancePackageBindingRecord(Base):
    """Owner-scoped package binding and effective config for one AgentInstance."""

    __tablename__ = "agent_instance_package_bindings"
    __table_args__ = (
        CheckConstraint(
            SHA256_SQL.format(column="effective_config_digest"), name="effective_config_digest_hex"
        ),
        CheckConstraint(
            SHA256_SQL.format(column="config_schema_digest"), name="config_schema_digest_hex"
        ),
        CheckConstraint("config_revision > 0", name="config_revision_positive"),
        CheckConstraint("updated_at >= created_at", name="timestamp_order"),
    )

    agent_instance_id: Mapped[int] = mapped_column(
        ForeignKey("agent_instances.id", ondelete="CASCADE"), primary_key=True
    )
    installed_package_version_id: Mapped[int] = mapped_column(
        ForeignKey("installed_package_versions.id", ondelete="RESTRICT"), nullable=False
    )
    effective_config_json: Mapped[str] = mapped_column(Text, nullable=False)
    effective_config_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    config_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    config_schema_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RunRecord(Base):
    """Persistence record for one immutable-snapshot Run."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint("agent_instance_id > 0", name="agent_instance_positive"),
        CheckConstraint(
            "status IN ('created','running','succeeded','failed','cancelled')", name="status_value"
        ),
        CheckConstraint(
            "input_max_bytes > 0 AND input_max_code_points > 0 AND output_max_bytes > 0 AND output_max_code_points > 0 AND provider_timeout_ms > 0 AND max_output_tokens > 0 AND max_model_calls > 0",
            name="limits_positive",
        ),
        # D1. A separate constraint rather than a widened `limits_positive`: the accepted C1
        # clause keeps its exact meaning, and the one Stage D limit that is legitimately zero
        # (`max_tool_calls`) sits outside it. `tool_grant_cutoff_id` is a monotonic grant id, not
        # a timestamp, and 0 means "admits no grant at all" -- which is what makes a migrated Run
        # incapable of acquiring a Stage D capability (ADR 0015).
        CheckConstraint(
            "max_tool_calls BETWEEN 0 AND 16 AND tool_timeout_ms BETWEEN 1000 AND 300000"
            " AND tool_result_max_bytes BETWEEN 1024 AND 1048576"
            " AND max_consecutive_tool_failures > 0 AND tool_grant_cutoff_id >= 0",
            name="tool_limits_bounds",
        ),
        CheckConstraint(
            f"length(input_text) BETWEEN 1 AND input_max_code_points AND length(CAST(input_text AS BLOB)) <= input_max_bytes AND instr(input_text, char(0)) = 0 AND length(trim(input_text, {NERVOS_BLANK_TEXT_SQL_CHARS})) > 0",
            name="input_bounds",
        ),
        CheckConstraint(
            "output_text IS NULL OR (length(output_text) BETWEEN 1 AND output_max_code_points AND length(CAST(output_text AS BLOB)) <= output_max_bytes AND instr(output_text, char(0)) = 0)",
            name="output_bounds",
        ),
        CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) AND (output_tokens IS NULL OR output_tokens >= 0) AND (total_tokens IS NULL OR total_tokens >= 0)",
            name="usage_nonnegative",
        ),
        CheckConstraint("elapsed_ms IS NULL OR elapsed_ms >= 0", name="elapsed_nonnegative"),
        CheckConstraint("started_at IS NULL OR started_at >= created_at", name="started_order"),
        CheckConstraint(
            "finished_at IS NULL OR (started_at IS NOT NULL AND finished_at >= started_at) OR (started_at IS NULL AND finished_at >= created_at AND (error_code = 'worker_recovery_exhausted' OR status = 'cancelled'))",
            name="finished_order",
        ),
        CheckConstraint(
            "(status='created' AND started_at IS NULL AND finished_at IS NULL AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NULL AND error_message IS NULL AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL AND elapsed_ms IS NULL) OR (status='running' AND started_at IS NOT NULL AND finished_at IS NULL AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NULL AND error_message IS NULL AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL AND elapsed_ms IS NULL) OR (status='succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL AND output_text IS NOT NULL AND length(trim(output_text, char(9,10,11,12,13,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288))) > 0 AND error_code IS NULL AND error_message IS NULL AND elapsed_ms IS NOT NULL) OR (status='failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NOT NULL AND error_code <> 'worker_recovery_exhausted' AND error_message IS NOT NULL AND length(trim(error_message, char(9,10,11,12,13,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288))) > 0 AND elapsed_ms IS NOT NULL) OR (status='failed' AND started_at IS NULL AND finished_at IS NOT NULL AND output_text IS NULL AND finish_reason IS NULL AND error_code = 'worker_recovery_exhausted' AND error_message IS NOT NULL AND length(trim(error_message, char(9,10,11,12,13,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288))) > 0 AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL AND elapsed_ms IS NULL) OR (status='cancelled' AND started_at IS NOT NULL AND finished_at IS NOT NULL AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NULL AND error_message IS NULL AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL AND elapsed_ms IS NOT NULL) OR (status='cancelled' AND started_at IS NULL AND finished_at IS NOT NULL AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NULL AND error_message IS NULL AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL AND elapsed_ms IS NULL)",
            name="lifecycle_shape",
        ),
        CheckConstraint(f"execution_kind IN ({EXECUTION_KINDS})", name="execution_kind_value"),
        CheckConstraint(
            "(execution_kind = 'builtin' AND installed_package_version_id IS NULL AND package_content_digest IS NULL AND package_environment_id IS NULL AND package_environment_digest IS NULL AND package_entrypoint IS NULL AND effective_config_json = '{}' AND effective_config_digest IS NULL AND agent_instance_config_revision IS NULL AND host_protocol_version IS NULL AND sdk_api_version IS NULL) OR (execution_kind = 'package' AND installed_package_version_id IS NOT NULL AND package_content_digest IS NOT NULL AND package_environment_id IS NOT NULL AND package_environment_digest IS NOT NULL AND package_entrypoint IS NOT NULL AND effective_config_digest IS NOT NULL AND agent_instance_config_revision IS NOT NULL AND host_protocol_version IS NOT NULL AND sdk_api_version IS NOT NULL)",
            name="execution_snapshot_shape",
        ),
        ForeignKeyConstraint(
            ["installed_package_version_id"],
            ["installed_package_versions.id"],
            name="fk_runs_installed_package_version_id_installed_package_versions",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["package_environment_id"],
            ["package_environments.id"],
            name="fk_runs_package_environment_id_package_environments",
            ondelete="RESTRICT",
        ),
        Index("ix_runs_agent_instance_id_id", "agent_instance_id", "id"),
        Index("ix_runs_installed_package_version_id", "installed_package_version_id"),
        Index("ix_runs_package_environment_id", "package_environment_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_instance_id: Mapped[int] = mapped_column(
        ForeignKey("agent_instances.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    agent_key: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_definition_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(256), nullable=False)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    input_max_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    input_max_code_points: Mapped[int] = mapped_column(Integer, nullable=False)
    output_max_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    output_max_code_points: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    max_model_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    # Stage D (D1) immutable Run limits. Every default is the legacy-compatible value, so a Run
    # submitted before tools existed -- and every Run C2 still submits -- carries 0 tool calls and
    # a cutoff of 0, and therefore cannot reach a tool even though the columns now exist.
    max_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tool_timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="30000")
    tool_result_max_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="65536"
    )
    max_consecutive_tool_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="3"
    )
    tool_grant_cutoff_id: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    execution_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="builtin"
    )
    installed_package_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    package_content_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    package_environment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    package_environment_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    package_entrypoint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    effective_config_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    effective_config_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_instance_config_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    host_protocol_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sdk_api_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class JobRecord(Base):
    """One durable execution obligation for a Run."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("run_id"),
        CheckConstraint(
            "status IN ('queued','claimed','running','retry_wait','succeeded','failed','cancelled')",
            name="status_value",
        ),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND max_attempts AND max_attempts BETWEEN 1 AND 10",
            name="attempt_bounds",
        ),
        CheckConstraint("length(model_provider) BETWEEN 1 AND 64", name="model_provider_shape"),
        CheckConstraint(
            "(claimed_by IS NULL AND claim_token IS NULL AND lease_expires_at IS NULL AND last_heartbeat_at IS NULL) OR (claimed_by IS NOT NULL AND length(claimed_by) BETWEEN 1 AND 128 AND claim_token IS NOT NULL AND length(claim_token)=32 AND lease_expires_at IS NOT NULL AND last_heartbeat_at IS NOT NULL)",
            name="claim_shape",
        ),
        CheckConstraint("(error_code IS NULL) = (error_message IS NULL)", name="error_pair"),
        CheckConstraint(
            "error_code IS NULL OR (length(error_code) BETWEEN 1 AND 64 AND length(error_message) BETWEEN 1 AND 512)",
            name="error_bounds",
        ),
        CheckConstraint(
            "updated_at >= created_at AND available_at >= created_at AND (finished_at IS NULL OR finished_at >= created_at) AND (cancel_requested_at IS NULL OR cancel_requested_at >= created_at)",
            name="timestamp_order",
        ),
        CheckConstraint(
            "(status IN ('succeeded','failed','cancelled')) = (finished_at IS NOT NULL)",
            name="terminal_finish",
        ),
        CheckConstraint(
            "(status IN ('failed','cancelled')) = (error_code IS NOT NULL)", name="terminal_error"
        ),
        CheckConstraint(
            "status != 'cancelled' OR cancel_requested_at IS NOT NULL", name="cancelled_request"
        ),
        Index("ix_jobs_status_available_at_id", "status", "available_at", "id"),
        Index("ix_jobs_agent_instance_id_status", "agent_instance_id", "status"),
        Index("ix_jobs_status_lease_expires_at_id", "status", "lease_expires_at", "id"),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False)
    agent_instance_id: Mapped[int] = mapped_column(
        ForeignKey("agent_instances.id", ondelete="RESTRICT"), nullable=False
    )
    model_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claim_token: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class JobAttemptRecord(Base):
    """One claim/execution episode for a Job."""

    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number"),
        CheckConstraint("attempt_number > 0", name="attempt_number_positive"),
        CheckConstraint(
            "status IN ('claimed','running','succeeded','failed','cancelled','expired')",
            name="status_value",
        ),
        CheckConstraint(
            "length(worker_id) BETWEEN 1 AND 128 AND length(claim_token)=32", name="owner_shape"
        ),
        CheckConstraint(
            "lease_expires_at > claimed_at AND last_heartbeat_at >= claimed_at AND created_at >= claimed_at",
            name="lease_order",
        ),
        CheckConstraint(
            "execution_started_at IS NULL OR execution_started_at >= claimed_at", name="start_order"
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= COALESCE(execution_started_at, claimed_at)",
            name="finish_order",
        ),
        CheckConstraint(
            "retry_disposition IS NULL OR retry_disposition IN ('SAFE_TO_RETRY','DO_NOT_RETRY','AMBIGUOUS')",
            name="retry_disposition_value",
        ),
        CheckConstraint("(error_code IS NULL) = (error_message IS NULL)", name="error_pair"),
        CheckConstraint(
            "error_code IS NULL OR (length(error_code) BETWEEN 1 AND 64 AND length(error_message) BETWEEN 1 AND 512)",
            name="error_bounds",
        ),
        CheckConstraint(
            "(status IN ('succeeded','failed','cancelled','expired')) = (finished_at IS NOT NULL)",
            name="terminal_finish",
        ),
        CheckConstraint(
            "status NOT IN ('claimed','running') OR (finished_at IS NULL AND retry_disposition IS NULL AND error_code IS NULL)",
            name="active_shape",
        ),
        CheckConstraint(
            "status != 'claimed' OR execution_started_at IS NULL", name="claimed_shape"
        ),
        CheckConstraint(
            "status != 'running' OR execution_started_at IS NOT NULL", name="running_shape"
        ),
        CheckConstraint(
            "status NOT IN ('failed','expired') OR retry_disposition IS NOT NULL",
            name="failure_disposition",
        ),
        CheckConstraint(
            "status != 'succeeded' OR (retry_disposition IS NULL AND error_code IS NULL)",
            name="success_shape",
        ),
        Index("ix_job_attempts_status_lease_expires_at_id", "status", "lease_expires_at", "id"),
        Index(
            "uq_job_attempts_one_active",
            "job_id",
            unique=True,
            sqlite_where=text("status IN ('claimed','running')"),
        ),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    claim_token: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    execution_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    lease_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    retry_disposition: Mapped[str | None] = mapped_column(String(20), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Stage D (D1) crash-durable aggregate usage for a multi-turn Attempt. They are NULL for every
    # Stage C Attempt, because a one-call Attempt reports its usage on the Run at terminalization
    # and nothing writes these until the tool loop exists (ADR 0017). No Run-level usage semantics
    # change.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RunEventRecord(Base):
    """Append-only safe lifecycle fact, sequenced within one Run."""

    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence"),
        CheckConstraint("sequence > 0", name="sequence_positive"),
        CheckConstraint(
            "event_type IN ('run.created','run.queued','attempt.claimed','attempt.started','attempt.failed','attempt.expired','retry.scheduled','cancellation.requested','run.cancelled','run.succeeded','run.failed','recovery.pre_start','recovery.ambiguous',"
            + TOOL_EVENT_TYPES
            + ")",
            name="event_type_value",
        ),
        CheckConstraint("(code IS NULL) = (message IS NULL)", name="message_pair"),
        CheckConstraint(
            "code IS NULL OR (length(code) BETWEEN 1 AND 64 AND length(message) BETWEEN 1 AND 512)",
            name="message_bounds",
        ),
        CheckConstraint(
            "attempt_number IS NULL OR attempt_number > 0", name="attempt_number_positive"
        ),
        CheckConstraint(
            "event_type != 'retry.scheduled' OR available_at IS NOT NULL", name="retry_available"
        ),
        Index("ix_run_events_attempt_id_id", "attempt_id", "id"),
        {"sqlite_autoincrement": True},
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False)
    attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_attempts.id", ondelete="RESTRICT"), nullable=True
    )
    tool_invocation_id: Mapped[int | None] = mapped_column(
        ForeignKey("tool_invocations.id", ondelete="RESTRICT"), nullable=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    attempt_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AuthSessionRecord(Base):
    """Persistence record for an opaque dashboard authentication session."""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash"),
        CheckConstraint("expires_at > created_at", name="expiration_order"),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="revocation_order",
        ),
        Index("ix_auth_sessions_user_id", "user_id"),
        Index("ix_auth_sessions_expires_at", "expires_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class WorkerRecord(Base):
    """Durable registry row for one Worker process incarnation (C3).

    This row is *observability*, not execution authority: an expired Job lease is the only
    signal that reclaims work. The registry records that an incarnation existed and when it
    was last seen, so an operator can tell a stopped incarnation from a crashed one.
    """

    __tablename__ = "workers"
    __table_args__ = (
        UniqueConstraint("worker_id"),
        CheckConstraint("length(worker_id) BETWEEN 1 AND 128", name="worker_id_shape"),
        CheckConstraint("last_heartbeat_at >= started_at", name="heartbeat_order"),
        CheckConstraint(
            "stopped_at IS NULL OR stopped_at >= last_heartbeat_at",
            name="stop_order",
        ),
        Index("ix_workers_last_heartbeat_at_id", "last_heartbeat_at", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Stage-H runtime qualification: the bounded public package-sandbox capability this
    # incarnation observed at startup. Observed health, never execution authority; a
    # Worker that loses its launcher is still refused by the launch factory per start.
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sandbox_backend: Mapped[str | None] = mapped_column(String(64), nullable=True)
    package_execution_supported: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


class QueuePartitionRecord(Base):
    """Durable fairness history for one Agent Instance partition (C6).

    This table is *scheduling metadata and nothing else*. It is not another queue, not a copy of
    Job state, not execution authority, and not an active or pending count. Jobs remain the only
    durable execution obligations, and the Job lease plus Attempt token remain the only authority
    to execute. The single fact stored here is the monotonically increasing Attempt identifier of
    the most recent committed claim for this partition, which is what makes
    least-recently-served ordering identical for every Worker, bounded in time (no history scan),
    and durable across a restart or a change in a Worker's provider capability set.

    `last_served_attempt_id` deliberately carries no foreign key. It is a monotone sequence
    marker that is only ever compared, never dereferenced: correctness does not require the
    referenced Attempt row to still be meaningful, and an FK would add a RESTRICT edge that
    would make pruning attempt history impossible for no scheduling benefit. A partition with no
    row, or with a NULL marker, has simply never been served.
    """

    __tablename__ = "queue_partitions"
    __table_args__ = (
        CheckConstraint("agent_instance_id > 0", name="agent_instance_positive"),
        CheckConstraint(
            "last_served_attempt_id IS NULL OR last_served_attempt_id > 0",
            name="last_served_positive",
        ),
    )

    agent_instance_id: Mapped[int] = mapped_column(
        ForeignKey("agent_instances.id", ondelete="RESTRICT"), primary_key=True, autoincrement=False
    )
    last_served_attempt_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ---------------------------------------------------------------------------------------------
# Stage D (D1) -- durable tool, capability, and audit schema.
#
# These four records are the *persistence foundation* of Stage D and nothing more: no tool is
# discovered, authorized, or executed by their existence. `mcp_connections` and
# `tool_definitions` are configuration, `agent_tool_grants` is the authority that exists now, and
# `tool_invocations` is the durable audit of calls that actually happened. Nothing here decides a
# permission; the call-time verdict is re-read from these rows by a later milestone.
# ---------------------------------------------------------------------------------------------


def _declared_name_sql(column: str) -> str:
    """SQL for an operator-declared lowercase name: a credential alias or a stdio server key.

    Lowercase kebab is frozen, which makes an environment-variable name -- the credential
    passthrough ADR 0016 forbids by name -- unrepresentable in either column rather than merely
    discouraged. The expression is NULL when the column is NULL, and a SQLite CHECK passes on
    NULL, so it is vacuously satisfied for a connection that declares neither.
    """
    return (
        f"{column} = trim({column}) AND length({column}) BETWEEN 1 AND 128"
        f" AND {column} NOT GLOB '*[^a-z0-9-]*' AND {column} GLOB '[a-z0-9]*'"
    )


def _sha256_hex_sql(column: str) -> str:
    """SQL for a lowercase 64-character sha256 hex digest, vacuous when the column is NULL."""
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


class McpConnectionRecord(Base):
    """Durable definition of one approved tool source.

    This row is *configuration*, never a live client. The MCP session is ephemeral and owned by the
    Worker, and no correctness property depends on it surviving (ADR 0016), so nothing here
    describes a connected socket, a negotiated revision, or a discovered catalog cache.

    **No column in this table can hold a credential value.** `credential_ref` is an opaque,
    operator-declared alias -- never an environment-variable name, and never the secret itself --
    and its shape is CHECK-constrained to lowercase, which is what makes an environment-variable
    name unrepresentable. The alias-to-environment mapping, the permitted target set, and the
    supported auth scheme live in operator-owned configuration, not in the database. Resolving the
    alias is a later milestone's job; D1 stores the reference and nothing that could leak.
    """

    __tablename__ = "mcp_connections"
    __table_args__ = (
        CheckConstraint("owner_user_id > 0", name="owner_positive"),
        CheckConstraint(
            "display_name = trim(display_name) AND length(display_name) BETWEEN 1 AND 100",
            name="display_name_shape",
        ),
        CheckConstraint("length(CAST(display_name AS BLOB)) <= 400", name="display_name_bytes"),
        CheckConstraint("transport IN ('stdio','http')", name="transport_value"),
        # Exactly one target per transport, and it is the one that transport uses. A stdio server
        # is named by its operator-declared key; an HTTP server by its origin. There is no column
        # for a command, an argument vector, or a working directory -- an arbitrary user-supplied
        # stdio command is arbitrary code execution with the Worker's privileges, so no migration
        # may ever add a field that accepts one (ADR 0016).
        CheckConstraint(
            "(transport = 'http' AND endpoint IS NOT NULL AND server_key IS NULL)"
            " OR (transport = 'stdio' AND endpoint IS NULL AND server_key IS NOT NULL)",
            name="target_shape",
        ),
        CheckConstraint(
            "endpoint IS NULL OR (length(endpoint) BETWEEN 1 AND 512"
            " AND instr(endpoint, char(0)) = 0)",
            name="endpoint_bounds",
        ),
        CheckConstraint(_declared_name_sql("server_key"), name="server_key_shape"),
        CheckConstraint(_declared_name_sql("credential_ref"), name="credential_ref_shape"),
        CheckConstraint("enabled IN (0, 1)", name="enabled_boolean"),
        CheckConstraint(f"catalog_status IN ({CONNECTION_STATUSES})", name="status_value"),
        CheckConstraint(
            "(last_error_code IS NULL) = (last_error_message IS NULL)", name="error_pair"
        ),
        CheckConstraint(
            "last_error_code IS NULL OR (length(last_error_code) BETWEEN 1 AND 64"
            " AND length(last_error_message) BETWEEN 1 AND 512)",
            name="error_bounds",
        ),
        CheckConstraint(
            "last_discovery_at IS NULL OR last_discovery_at >= created_at", name="discovery_order"
        ),
        CheckConstraint("updated_at >= created_at", name="timestamp_order"),
        Index("ix_mcp_connections_owner_user_id_id", "owner_user_id", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    transport: Mapped[str] = mapped_column(String(16), nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    server_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    credential_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    # A connection that has not proven itself connected offers no tools. The default is therefore
    # the fail-closed state rather than an optimistic one, and `disabled` is the only status the
    # owner flips directly.
    catalog_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="unavailable"
    )
    last_discovery_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ToolDefinitionRecord(Base):
    """Durable description of one callable operation exposed by one source.

    Identity is `(source_kind, source_id, upstream_name)`, and it is enforced by two **partial
    unique indexes** plus a pairing CHECK rather than by one three-column `UNIQUE`. That is not
    stylistic: SQLite does not enforce uniqueness over a NULL, so a plain
    `UNIQUE(source_kind, source_id, upstream_name)` would accept unlimited duplicate built-ins --
    the case where `source_id IS NULL`. The partial indexes are what make the built-in case as
    strict as the MCP one.

    `hint_*` columns hold the source's own annotation claims. They are **untrusted hints and
    presentation metadata only**: a server can lie, so they may never grant authority, remove
    authority, permit an automatic retry, or satisfy any predicate in a permission decision
    (ADR 0015). Their defaults are the conservative reading the MCP specification itself
    prescribes -- `destructive` and `open_world` default true -- so an unannotated tool is stored
    as possibly destructive and possibly open-world. They are persisted because they are part of
    the fingerprint the user reviewed, not because they are trusted.
    """

    __tablename__ = "tool_definitions"
    __table_args__ = (
        # The pairing CHECK is what keeps the two partial unique indexes total: without it a
        # `builtin` row with a non-null `source_id` would fall outside both indexes and escape
        # uniqueness entirely.
        CheckConstraint(
            "(source_kind = 'builtin' AND source_id IS NULL)"
            " OR (source_kind = 'mcp' AND source_id IS NOT NULL)",
            name="source_shape",
        ),
        CheckConstraint("source_kind IN ('builtin','mcp')", name="source_kind_value"),
        CheckConstraint("length(upstream_name) BETWEEN 1 AND 128", name="upstream_name_shape"),
        CheckConstraint(MODEL_NAME_SQL, name="model_name_shape"),
        CheckConstraint(
            "display_name = trim(display_name) AND length(display_name) BETWEEN 1 AND 100",
            name="display_name_shape",
        ),
        CheckConstraint("length(CAST(display_name AS BLOB)) <= 400", name="display_name_bytes"),
        # A tool description is untrusted text from an external server, so it is bounded: it may
        # never be large enough to flood a model's context on its own.
        CheckConstraint("length(CAST(description AS BLOB)) <= 65536", name="description_bounds"),
        CheckConstraint(
            "length(CAST(input_schema AS BLOB)) BETWEEN 2 AND 65536", name="input_schema_bounds"
        ),
        CheckConstraint(
            "output_schema IS NULL OR length(CAST(output_schema AS BLOB)) BETWEEN 2 AND 65536",
            name="output_schema_bounds",
        ),
        CheckConstraint(_sha256_hex_sql("fingerprint"), name="fingerprint_shape"),
        CheckConstraint(f"status IN ({DEFINITION_STATUSES})", name="status_value"),
        CheckConstraint(
            "hint_read_only IN (0, 1) AND hint_destructive IN (0, 1)"
            " AND hint_idempotent IN (0, 1) AND hint_open_world IN (0, 1)",
            name="hint_boolean",
        ),
        CheckConstraint("updated_at >= created_at", name="timestamp_order"),
        Index(
            "uq_tool_definitions_builtin",
            "upstream_name",
            unique=True,
            sqlite_where=text("source_kind = 'builtin'"),
        ),
        Index(
            "uq_tool_definitions_mcp",
            "source_id",
            "upstream_name",
            unique=True,
            sqlite_where=text("source_kind = 'mcp'"),
        ),
        Index("uq_tool_definitions_model_name", "model_name", unique=True),
        # Not redundant with the partial MCP index above: SQLite cannot use a partial index
        # unless the query's own WHERE clause implies the index's condition, so a lookup by
        # `source_id` alone would have to scan. This is the index that serves "the definitions of
        # one connection".
        Index("ix_tool_definitions_source_id", "source_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("mcp_connections.id", ondelete="RESTRICT"), nullable=True
    )
    upstream_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[str] = mapped_column(Text, nullable=False)
    output_schema: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    hint_read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    hint_destructive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")
    hint_idempotent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    hint_open_world: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AgentToolGrantRecord(Base):
    """The durable authority to invoke one Tool Definition from one Agent Instance.

    **The row's existence is the authority.** There is no DENY row in Stage D, so "never granted"
    and "revoked" are the same observable state, and revocation is a delete. A drifted definition
    fails closed at the next call-time check instead of being denied by a row.

    `id` carries two roles at once: it is the grant's durable identity, and it is the monotonic
    primitive `runs.tool_grant_cutoff_id` snapshots at submission. AUTOINCREMENT is therefore
    required, and ids are never reused -- re-granting and re-confirming each mint a new row, so a
    capability can never be silently acquired by a Run whose cutoff predates the review. That is
    also why `created_at` is
    display metadata only: **no timestamp decides authority anywhere in this model.**
    """

    __tablename__ = "agent_tool_grants"
    __table_args__ = (
        UniqueConstraint("agent_instance_id", "tool_definition_id"),
        CheckConstraint("agent_instance_id > 0", name="agent_instance_positive"),
        CheckConstraint("tool_definition_id > 0", name="tool_definition_positive"),
        CheckConstraint(_sha256_hex_sql("reviewed_fingerprint"), name="reviewed_fingerprint_shape"),
        Index("ix_agent_tool_grants_tool_definition_id", "tool_definition_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_instance_id: Mapped[int] = mapped_column(
        ForeignKey("agent_instances.id", ondelete="RESTRICT"), nullable=False
    )
    tool_definition_id: Mapped[int] = mapped_column(
        ForeignKey("tool_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    # The fingerprint the user actually reviewed. Immutable for the life of the row, because
    # re-confirming a drifted definition replaces the row rather than editing this value.
    reviewed_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class TriggerDefinitionRecord(Base):
    """Current owner-scoped TriggerDefinition configuration."""

    __tablename__ = "trigger_definitions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('one_time','interval','cron','webhook','event')",
            name="kind_value",
        ),
        CheckConstraint(
            "misfire_policy IN ('coalesce_one')",
            name="misfire_policy_value",
        ),
        CheckConstraint("enabled IN (0, 1)", name="enabled_value"),
        CheckConstraint("config_revision > 0", name="config_revision_positive"),
        CheckConstraint(
            "length(display_name) BETWEEN 1 AND 400 AND instr(display_name, char(0)) = 0",
            name="display_name_shape",
        ),
        CheckConstraint(
            "length(input_text) BETWEEN 1 AND 4000 AND "
            "length(CAST(input_text AS BLOB)) <= 8000 AND instr(input_text, char(0)) = 0 AND "
            f"length(trim(input_text, {NERVOS_BLANK_TEXT_SQL_CHARS})) > 0",
            name="input_bounds",
        ),
        CheckConstraint(
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
        CheckConstraint(
            "((kind IN ('one_time','interval','cron') AND "
            "((enabled = 1 AND next_fire_at IS NOT NULL) OR "
            "(enabled = 0 AND next_fire_at IS NULL))) OR "
            "(kind IN ('webhook','event') AND next_fire_at IS NULL))",
            name="next_fire_alignment",
        ),
        CheckConstraint(
            "interval_seconds IS NULL OR interval_seconds BETWEEN 60 AND 31536000",
            name="interval_bounds",
        ),
        CheckConstraint(
            "cron_expression IS NULL OR (length(cron_expression) BETWEEN 1 AND 128 "
            "AND instr(cron_expression, char(0)) = 0)",
            name="cron_expression_shape",
        ),
        CheckConstraint(
            "timezone IS NULL OR (length(timezone) BETWEEN 1 AND 64 AND instr(timezone, char(0)) = 0)",
            name="timezone_shape",
        ),
        CheckConstraint(
            "public_id IS NULL OR (length(public_id) = 22 AND "
            "public_id NOT GLOB '*[^A-Za-z0-9_-]*' AND public_id GLOB '[A-Za-z0-9_-]*' "
            "AND instr(public_id, char(0)) = 0)",
            name="public_id_shape",
        ),
        CheckConstraint(
            "secret_digest IS NULL OR length(secret_digest) = 32",
            name="secret_digest_shape",
        ),
        CheckConstraint(
            "(secret_digest IS NULL) = (secret_created_at IS NULL)",
            name="secret_pair",
        ),
        CheckConstraint(
            "event_type IS NULL OR (length(event_type) BETWEEN 1 AND 64 "
            "AND instr(event_type, char(0)) = 0 AND event_type = lower(event_type) "
            "AND event_type NOT GLOB '*[^a-z0-9_.]*' AND event_type NOT GLOB '.*' "
            "AND event_type NOT GLOB '*.' AND event_type NOT GLOB '*..*' "
            "AND event_type NOT LIKE 'nervos.%' AND event_type GLOB '[a-z]*')",
            name="event_type_shape",
        ),
        CheckConstraint("updated_at >= created_at", name="timestamp_order"),
        ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], name="owner_user_id_users", ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["agent_instance_id"],
            ["agent_instances.id"],
            name="agent_instance_id_agent_instances",
            ondelete="RESTRICT",
        ),
        Index("ix_trigger_definitions_owner_user_id_id", "owner_user_id", "id"),
        Index("ix_trigger_definitions_agent_instance_id_id", "agent_instance_id", "id"),
        Index(
            "ix_trigger_definitions_next_fire_at_id",
            "next_fire_at",
            "id",
            sqlite_where=text("next_fire_at IS NOT NULL"),
        ),
        Index(
            "ix_trigger_definitions_event_lookup",
            "owner_user_id",
            "event_type",
            sqlite_where=text("event_type IS NOT NULL"),
        ),
        Index(
            "ux_trigger_definitions_public_id",
            "public_id",
            unique=True,
            sqlite_where=text("public_id IS NOT NULL"),
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_instance_id: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    display_name: Mapped[str] = mapped_column(String(400), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    config_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    misfire_policy: Mapped[str] = mapped_column(String(16), nullable=False)
    next_fire_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cron_expression: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    public_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    secret_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)
    secret_created_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    event_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class TriggerOccurrenceRecord(Base):
    """Immutable outcome of one trigger delivery or due instant."""

    __tablename__ = "trigger_occurrences"
    __table_args__ = (
        CheckConstraint("status IN ('run_created','skipped')", name="status_value"),
        CheckConstraint("trigger_revision > 0", name="trigger_revision_positive"),
        CheckConstraint(
            "((status = 'run_created' AND run_id IS NOT NULL AND skip_code IS NULL "
            "AND skip_message IS NULL) OR (status = 'skipped' AND run_id IS NULL "
            "AND skip_code IS NOT NULL AND skip_message IS NOT NULL))",
            name="status_shape",
        ),
        CheckConstraint(
            "skip_code IS NULL OR (length(skip_code) BETWEEN 1 AND 64 AND "
            "length(skip_message) BETWEEN 1 AND 512 AND instr(skip_message, char(0)) = 0 "
            f"AND length(trim(skip_message, {NERVOS_BLANK_TEXT_SQL_CHARS})) > 0)",
            name="skip_bounds",
        ),
        CheckConstraint(
            "payload_bytes IS NULL OR payload_bytes >= 0", name="payload_bytes_nonnegative"
        ),
        CheckConstraint("(payload_digest IS NULL) = (payload_bytes IS NULL)", name="payload_pair"),
        CheckConstraint(
            "payload_digest IS NULL OR length(payload_digest) = 32", name="payload_digest_shape"
        ),
        CheckConstraint(
            "event_id IS NULL OR (length(event_id) BETWEEN 1 AND 128 AND instr(event_id, char(0)) = 0)",
            name="event_id_shape",
        ),
        CheckConstraint(
            "idempotency_key IS NULL OR (length(idempotency_key) BETWEEN 1 AND 128 "
            "AND instr(idempotency_key, char(0)) = 0)",
            name="idempotency_key_shape",
        ),
        CheckConstraint(
            "((nominal_at IS NOT NULL AND event_id IS NULL AND idempotency_key IS NULL) OR "
            "(nominal_at IS NULL AND event_id IS NOT NULL AND idempotency_key IS NULL) OR "
            "(nominal_at IS NULL AND event_id IS NULL AND idempotency_key IS NOT NULL) OR "
            "(nominal_at IS NULL AND event_id IS NULL AND idempotency_key IS NULL))",
            name="identity_shape",
        ),
        CheckConstraint("created_at >= occurred_at", name="occurred_order"),
        ForeignKeyConstraint(
            ["trigger_definition_id"],
            ["trigger_definitions.id"],
            name="trigger_definition_id_trigger_definitions",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["owner_user_id"], ["users.id"], name="owner_user_id_users", ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["agent_instance_id"],
            ["agent_instances.id"],
            name="agent_instance_id_agent_instances",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["run_id"], ["runs.id"], name="run_id_runs", ondelete="RESTRICT"),
        Index("ix_trigger_occurrences_trigger_definition_id_id", "trigger_definition_id", "id"),
        Index("ix_trigger_occurrences_owner_user_id_id", "owner_user_id", "id"),
        Index(
            "ux_trigger_occurrences_run_id",
            "run_id",
            unique=True,
            sqlite_where=text("run_id IS NOT NULL"),
        ),
        Index(
            "ux_trigger_occurrences_schedule_identity",
            "trigger_definition_id",
            "nominal_at",
            unique=True,
            sqlite_where=text("nominal_at IS NOT NULL"),
        ),
        Index(
            "ux_trigger_occurrences_event_identity",
            "trigger_definition_id",
            "event_id",
            unique=True,
            sqlite_where=text("event_id IS NOT NULL"),
        ),
        Index(
            "ux_trigger_occurrences_idempotency_identity",
            "trigger_definition_id",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger_definition_id: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_instance_id: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    skip_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skip_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    nominal_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)
    payload_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


# ============================================================================================
# F1: Conversations, Turns, Messages, Run Links
# ============================================================================================


class ConversationRecord(Base):
    """Owner-scoped durable conversation belonging to one AgentInstance."""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            "title IS NULL OR (length(title) BETWEEN 1 AND 400 AND instr(title, char(0)) = 0)",
            name="title_shape",
        ),
        CheckConstraint("status IN ('active','archived','deleted')", name="status_value"),
        CheckConstraint(
            "((status = 'active' AND archived_at IS NULL AND deleted_at IS NULL) OR "
            "(status = 'archived' AND archived_at IS NOT NULL AND deleted_at IS NULL) OR "
            "(status = 'deleted' AND deleted_at IS NOT NULL))",
            name="lifecycle_timestamps",
        ),
        CheckConstraint("updated_at >= created_at", name="timestamp_order"),
        ForeignKeyConstraint(
            ["agent_instance_id"],
            ["agent_instances.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        PrimaryKeyConstraint("id"),
        Index("ix_conversations_owner_id", "owner_user_id", "id"),
        Index("ix_conversations_owner_agent", "owner_user_id", "agent_instance_id"),
        Index("ix_conversations_owner_status_id", "owner_user_id", "status", "id"),
        Index(
            "ix_conversations_owner_agent_status_id",
            "owner_user_id",
            "agent_instance_id",
            "status",
            "id",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_instance_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(400), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ConversationTurnRecord(Base):
    """One submission/execution lifecycle within a Conversation."""

    __tablename__ = "conversation_turns"
    __table_args__ = (
        CheckConstraint("sequence > 0", name="sequence_positive"),
        CheckConstraint(
            "state IN ('pending','running','succeeded','failed','cancelled','ambiguous')",
            name="state_value",
        ),
        CheckConstraint(
            "length(client_message_id) BETWEEN 1 AND 128 AND instr(client_message_id, char(0)) = 0",
            name="client_message_id_shape",
        ),
        CheckConstraint(
            "length(content_digest) = 32",
            name="content_digest_length",
        ),
        ForeignKeyConstraint(["authoritative_run_id"], ["runs.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="RESTRICT"),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("conversation_id", "sequence", name="uq_conversation_turns_sequence"),
        UniqueConstraint(
            "conversation_id",
            "client_message_id",
            name="uq_conversation_turns_client_message_id",
        ),
        Index(
            "uq_conversation_turns_one_active",
            "conversation_id",
            unique=True,
            sqlite_where=text("state IN ('pending','running')"),
        ),
        Index(
            "ix_conversation_turns_conversation_sequence",
            "conversation_id",
            "sequence",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    client_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    authoritative_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ConversationRunLinkRecord(Base):
    """Link from a Turn to an ordinary Run (initial or retry)."""

    __tablename__ = "conversation_run_links"
    __table_args__ = (
        CheckConstraint("ordinal >= 1", name="ordinal_positive"),
        CheckConstraint("role IN ('initial','retry')", name="link_role_value"),
        CheckConstraint(
            "context_mode IN ('f1_single_turn','f2_context_snapshot')",
            name="context_mode_value",
        ),
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["turn_id"], ["conversation_turns.id"], ondelete="RESTRICT"),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("run_id", name="uq_conversation_run_links_run"),
        UniqueConstraint("turn_id", "ordinal", name="uq_conversation_run_links_turn_ordinal"),
        UniqueConstraint("turn_id", "run_id", name="uq_conversation_run_links_turn_run"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    turn_id: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    context_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="f1_single_turn"
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RunContextSnapshotRecord(Base):
    """Immutable provider-neutral context snapshot for an F2 conversational Run."""

    __tablename__ = "run_context_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["turn_id"], ["conversation_turns.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["turn_id", "run_id"],
            ["conversation_run_links.turn_id", "conversation_run_links.run_id"],
            ondelete="RESTRICT",
        ),
        PrimaryKeyConstraint("run_id"),
        CheckConstraint("schema_version > 0", name="schema_version_positive"),
        CheckConstraint("length(builder_version) BETWEEN 1 AND 64", name="builder_version_length"),
        CheckConstraint(
            "max_total_bytes > 0 AND max_total_code_points > 0", name="max_bounds_positive"
        ),
        CheckConstraint(
            "actual_total_bytes >= 0 AND actual_total_code_points >= 0",
            name="actual_counts_nonnegative",
        ),
        CheckConstraint(
            "actual_total_bytes <= max_total_bytes AND actual_total_code_points <= max_total_code_points",
            name="actual_within_bounds",
        ),
        CheckConstraint(
            "compaction_version IS NULL OR (compaction_source_start IS NOT NULL AND compaction_source_end IS NOT NULL AND compaction_source_start <= compaction_source_end AND compaction_source_start > 0)",
            name="compaction_provenance_valid",
        ),
        CheckConstraint("length(content_digest) = 32", name="content_digest_length"),
        Index("ix_run_context_snapshots_turn_id", "turn_id"),
        {"sqlite_autoincrement": False},
    )

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    turn_id: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    builder_version: Mapped[str] = mapped_column(String(64), nullable=False)
    current_user_text: Mapped[str] = mapped_column(Text, nullable=False)
    history_messages: Mapped[str] = mapped_column(Text, nullable=False)
    selected_turn_ids: Mapped[str] = mapped_column(Text, nullable=False)
    selected_message_ids: Mapped[str] = mapped_column(Text, nullable=False)
    compaction_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compaction_source_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    compaction_source_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    injected_compaction_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_key: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_definition_version: Mapped[str] = mapped_column(String(64), nullable=False)
    max_total_bytes: Mapped[int] = mapped_column(Integer, nullable=False, server_default="8000")
    max_total_code_points: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="4000"
    )
    actual_total_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_total_code_points: Mapped[int] = mapped_column(Integer, nullable=False)
    rendered_context: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    memory_items_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    injected_user_memory_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    injected_agent_memory_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConversationCompactionRecord(Base):
    """Deterministic non-model compaction of older committed conversation turns."""

    __tablename__ = "conversation_compactions"
    __table_args__ = (
        ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="RESTRICT"),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("conversation_id", "version", name="uq_conversation_compactions_version"),
        CheckConstraint("version > 0", name="compaction_version_positive"),
        CheckConstraint(
            "source_start_sequence > 0 AND source_end_sequence >= source_start_sequence",
            name="compaction_sequence_range",
        ),
        CheckConstraint(
            "length(content) BETWEEN 1 AND 32000 AND length(CAST(content AS BLOB)) <= 32000",
            name="compaction_content_bounds",
        ),
        CheckConstraint("length(content_digest) = 32", name="compaction_digest_length"),
        Index(
            "uq_conversation_compactions_one_current",
            "conversation_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_start_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_end_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class MemoryItemRecord(Base):
    """Durable memory item identity and current-version pointer."""

    __tablename__ = "memory_items"
    __table_args__ = (
        ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["agent_instance_id"], ["agent_instances.id"], ondelete="RESTRICT"),
        PrimaryKeyConstraint("id"),
        CheckConstraint("scope IN ('user','agent')", name="scope_value"),
        CheckConstraint(
            "(scope = 'user' AND agent_instance_id IS NULL) OR "
            "(scope = 'agent' AND agent_instance_id IS NOT NULL)",
            name="scope_agent_shape",
        ),
        CheckConstraint("status IN ('active','deleted')", name="status_value"),
        CheckConstraint("current_version > 0", name="current_version_positive"),
        CheckConstraint("updated_at >= created_at", name="timestamp_order"),
        Index(
            "ix_memory_items_owner_scope_status",
            "owner_user_id",
            "scope",
            "status",
            "id",
        ),
        Index(
            "ix_memory_items_owner_agent_status",
            "owner_user_id",
            "agent_instance_id",
            "status",
            "id",
            sqlite_where=text("agent_instance_id IS NOT NULL"),
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class MemoryVersionRecord(Base):
    """One immutable version of a memory item."""

    __tablename__ = "memory_versions"
    __table_args__ = (
        ForeignKeyConstraint(["memory_item_id"], ["memory_items.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("memory_item_id", "version", name="uq_memory_versions_item_version"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint(
            f"length(content) >= 1 AND length(CAST(content AS BLOB)) <= 32000 AND instr(content, char(0)) = 0 AND length(trim(content, {NERVOS_BLANK_TEXT_SQL_CHARS})) > 0",
            name="content_bounds",
        ),
        CheckConstraint("length(content_digest) = 32", name="content_digest_length"),
        CheckConstraint(
            "(source_kind = 'direct_user' AND source_id IS NULL) OR "
            "(source_kind IN ('promoted_message','promoted_run') AND source_id IS NOT NULL)",
            name="source_shape",
        ),
        CheckConstraint(
            "source_kind IN ('direct_user','promoted_message','promoted_run')",
            name="source_kind_value",
        ),
        CheckConstraint(
            "provenance_type IN ('user_authored','user_approved_inferred')",
            name="provenance_type_value",
        ),
        Index("ix_memory_versions_item_version", "memory_item_id", "version"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    memory_item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provenance_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ConversationMessageRecord(Base):
    """One USER or ASSISTANT message attached to a Turn."""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user','assistant')", name="role_value"),
        CheckConstraint(
            "length(content) BETWEEN 1 AND 32000 AND length(CAST(content AS BLOB)) <= 32000 AND instr(content, char(0)) = 0",
            name="message_content_bounds",
        ),
        ForeignKeyConstraint(
            ["turn_id", "source_run_id"],
            ["conversation_run_links.turn_id", "conversation_run_links.run_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(["turn_id"], ["conversation_turns.id"], ondelete="RESTRICT"),
        PrimaryKeyConstraint("id"),
        UniqueConstraint("turn_id", "role", name="uq_conversation_messages_turn_role"),
        CheckConstraint(
            "(role = 'user' AND source_run_id IS NULL) OR "
            "(role = 'assistant' AND source_run_id IS NOT NULL)",
            name="message_role_source",
        ),
        Index(
            "uq_conversation_messages_source_run",
            "source_run_id",
            unique=True,
            sqlite_where=text("source_run_id IS NOT NULL"),
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    turn_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ToolInvocationRecord(Base):
    """The authoritative durable audit of one tool call, reachable through its Run.

    Four properties of this table are load-bearing and are enforced by the schema rather than by
    convention.

    **It cannot hold a secret.** There is no column for an argument, a result, a credential, a
    token, a claim token, or a raw provider payload. Arguments and results live in Worker memory
    for one Attempt and are represented here only by a digest, a byte count, and -- for arguments
    -- a bounded key/type skeleton carrying no values. A digest is **content evidence, never
    identity**: it deduplicates nothing, keys nothing, and permits no replay. Invocation identity
    is `id`, and ordering is `tool_sequence`.

    **`started_at` is the ambiguity boundary.** It is committed immediately before dispatch, so a
    row with `started_at` set means the call may already have reached an external system and may
    never be replayed (ADR 0017). The lifecycle CHECK keeps the non-dispatched states
    (`requested`, `denied`, `cancelled`) permanently incapable of claiming a start.

    **History survives deletion.** `source_kind`, `source_id`, `upstream_name` and `model_name`
    are copied at call time, so this row stays readable and self-describing after the definition
    or even the connection is gone -- which is why `source_id` deliberately carries **no** foreign
    key. The FK to `tool_definitions` is RESTRICT, so a definition with invocation history can
    never be cascaded away.

    **Failure is explainable.** A dispatched call that cannot be concluded is `ambiguous` and must
    carry a safe error pair; there is no "outcome unknown" state that invites a retry.
    """

    __tablename__ = "tool_invocations"
    __table_args__ = (
        UniqueConstraint("attempt_id", "tool_sequence"),
        CheckConstraint("tool_sequence > 0", name="tool_sequence_positive"),
        CheckConstraint(
            f"status IN ({INVOCATION_STATUSES})",
            name="status_value",
        ),
        CheckConstraint(
            "(source_kind = 'builtin' AND source_id IS NULL)"
            " OR (source_kind = 'mcp' AND source_id IS NOT NULL)",
            name="source_shape",
        ),
        CheckConstraint("source_kind IN ('builtin','mcp')", name="source_kind_value"),
        CheckConstraint("length(upstream_name) BETWEEN 1 AND 128", name="upstream_name_shape"),
        CheckConstraint(MODEL_NAME_SQL, name="model_name_shape"),
        CheckConstraint(
            _sha256_hex_sql("definition_fingerprint"), name="definition_fingerprint_shape"
        ),
        # The permission decision is durable: "was this call allowed?" is answerable after the
        # fact for every call that happened. The reason vocabulary is `denied_<reason>`, left
        # deliberately open because the evaluator that produces the reasons is a later milestone.
        CheckConstraint(
            "permission_decision = 'allowed' OR permission_decision GLOB 'denied_*'",
            name="permission_decision_value",
        ),
        CheckConstraint(
            "length(permission_decision) BETWEEN 1 AND 64", name="permission_decision_bounds"
        ),
        CheckConstraint(
            "provider_call_id IS NULL OR length(provider_call_id) BETWEEN 1 AND 128",
            name="provider_call_id_bounds",
        ),
        # The ambiguity boundary, stated as a durable invariant: an invocation is dispatched
        # exactly when it has a start, and never otherwise.
        CheckConstraint(
            f"(status IN ({INVOCATION_DISPATCHED_STATUSES})) = (started_at IS NOT NULL)",
            name="dispatched_shape",
        ),
        # The full status/timestamp shape, in the same discipline as `runs.lifecycle_shape`. A
        # non-dispatched terminal state (`denied`, `cancelled`) is finished but never started; a
        # dispatched one is always started. There is no representable state where a call both
        # never started and never finished except `requested`, which is the intent that precedes
        # every one of them.
        CheckConstraint(
            "(status = 'requested' AND started_at IS NULL AND finished_at IS NULL)"
            " OR (status IN ('denied','cancelled') AND started_at IS NULL"
            " AND finished_at IS NOT NULL)"
            " OR (status = 'started' AND started_at IS NOT NULL AND finished_at IS NULL)"
            " OR (status IN ('succeeded','failed','ambiguous') AND started_at IS NOT NULL"
            " AND finished_at IS NOT NULL)",
            name="lifecycle_shape",
        ),
        CheckConstraint("started_at IS NULL OR started_at >= requested_at", name="start_order"),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= COALESCE(started_at, requested_at)",
            name="finish_order",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND result_digest IS NOT NULL AND result_bytes IS NOT NULL"
            " AND result_truncated IS NOT NULL)"
            " OR (status != 'succeeded' AND result_digest IS NULL AND result_bytes IS NULL"
            " AND result_truncated IS NULL)",
            name="result_shape",
        ),
        CheckConstraint("result_bytes IS NULL OR result_bytes >= 0", name="result_nonnegative"),
        CheckConstraint(
            "result_truncated IS NULL OR result_truncated IN (0, 1)", name="result_truncated_value"
        ),
        CheckConstraint(_sha256_hex_sql("arguments_digest"), name="arguments_digest_shape"),
        CheckConstraint(_sha256_hex_sql("result_digest"), name="result_digest_shape"),
        CheckConstraint(
            "arguments_shape IS NULL OR length(CAST(arguments_shape AS BLOB)) <= 4096",
            name="arguments_shape_bounds",
        ),
        CheckConstraint("status != 'succeeded' OR error_code IS NULL", name="success_shape"),
        CheckConstraint(
            "status NOT IN ('failed','ambiguous') OR error_code IS NOT NULL",
            name="failure_error",
        ),
        CheckConstraint("(error_code IS NULL) = (error_message IS NULL)", name="error_pair"),
        CheckConstraint(
            "error_code IS NULL OR (length(error_code) BETWEEN 1 AND 64"
            " AND length(error_message) BETWEEN 1 AND 512)",
            name="error_bounds",
        ),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="RESTRICT"), nullable=False)
    attempt_id: Mapped[int] = mapped_column(
        ForeignKey("job_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    tool_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_definition_id: Mapped[int] = mapped_column(
        ForeignKey("tool_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upstream_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    definition_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    permission_decision: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    arguments_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    arguments_shape: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_truncated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)


# --- Stage H: security isolation (ADRs 0032-0036) -------------------------------------------

# H1: encrypted secret metadata. Ciphertext is opaque AES-256-GCM material bound to the row
# identity by AAD; no column anywhere carries plaintext. `revoked` rows keep only evidence.
# The master key lives outside the database; `secret_keys` records only its identity.


class SecretRecord(Base):
    __tablename__ = "secrets"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "name", name="owner_secret_name"),
        CheckConstraint("status IN ('active','disabled','revoked')", name="status_value"),
        CheckConstraint("length(name) BETWEEN 1 AND 128", name="name_bound"),
        CheckConstraint("key_version >= 1", name="key_version_positive"),
        CheckConstraint("rotation_count >= 0", name="rotation_nonnegative"),
        CheckConstraint(
            "(status = 'revoked' AND length(CAST(ciphertext AS BLOB)) = 0)"
            " OR (status != 'revoked' AND length(CAST(ciphertext AS BLOB)) > 0)",
            name="revocation_destroys_value",
        ),
        Index("ix_secrets_owner_id", "owner_user_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(16), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    rotation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class SecretKeyVersionRecord(Base):
    __tablename__ = "secret_keys"
    key_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


# H2: a user's connection to one external account provider. Tokens live only in `secrets`
# through `secret_id`; this row carries identity, scopes, and lifecycle metadata only.

CONNECTION_CONNECTION_STATUSES = "'connected','needs_refresh','disconnected','revoked'"


class AccountConnectionRecord(Base):
    __tablename__ = "account_connections"
    __table_args__ = (
        CheckConstraint(f"state IN ({CONNECTION_CONNECTION_STATUSES})", name="state_value"),
        CheckConstraint("length(provider) BETWEEN 1 AND 64", name="provider_bound"),
        CheckConstraint("length(scopes_json) <= 4096", name="scopes_bound"),
        CheckConstraint("length(display_name) BETWEEN 1 AND 128", name="display_name_bound"),
        Index("ix_account_connections_owner_id", "owner_user_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    secret_id: Mapped[int] = mapped_column(ForeignKey("secrets.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    scopes_json: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    refresh_failed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    refresh_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    refresh_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    revocation_outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


# H3: durable per-action approval. One row authorizes at most one dispatch of one exact
# action input on one Attempt; every other path to a terminal state dispatches nothing.


class AccountOAuthRequestRecord(Base):
    __tablename__ = "account_oauth_requests"
    __table_args__ = (
        CheckConstraint(
            "length(state_hash) = 64 AND state_hash NOT GLOB '*[^0-9a-f]*'", name="state_hash_shape"
        ),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        Index("ix_account_oauth_requests_owner_expiry", "owner_user_id", "expires_at"),
    )
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_id: Mapped[int] = mapped_column(ForeignKey("secrets.id"), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes_json: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


APPROVAL_STATES = "'pending','approved','consumed','denied','expired','cancelled','revoked'"


class ActionApprovalRecord(Base):
    __tablename__ = "action_approvals"
    __table_args__ = (
        CheckConstraint(f"state IN ({APPROVAL_STATES})", name="state_value"),
        CheckConstraint(
            "length(fingerprint) = 64 AND fingerprint NOT GLOB '*[^0-9a-f]*'",
            name="fingerprint_shape",
        ),
        CheckConstraint(
            "length(input_digest) = 64 AND input_digest NOT GLOB '*[^0-9a-f]*'",
            name="input_digest_shape",
        ),
        CheckConstraint("expires_at > requested_at", name="expiry_after_request"),
        CheckConstraint(
            "(state = 'pending' AND decided_at IS NULL AND consumed_at IS NULL)"
            " OR (state = 'approved' AND decided_at IS NOT NULL AND consumed_at IS NULL)"
            " OR (state = 'consumed' AND decided_at IS NOT NULL AND consumed_at IS NOT NULL)"
            " OR (state IN ('denied','expired','cancelled','revoked') AND consumed_at IS NULL)",
            name="lifecycle_shape",
        ),
        UniqueConstraint("attempt_id", "tool_sequence", name="attempt_sequence"),
        Index("ix_action_approvals_owner_state", "owner_user_id", "state", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    agent_instance_id: Mapped[int] = mapped_column(ForeignKey("agent_instances.id"), nullable=False)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("job_attempts.id"), nullable=False)
    tool_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_definition_id: Mapped[int] = mapped_column(
        ForeignKey("tool_definitions.id"), nullable=False
    )
    upstream_name: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


WORKFLOW_STATUSES = (
    "'pending','runnable','running','waiting','succeeded','failed','cancelled','needs_review'"
)
WORKFLOW_WAIT_KINDS = "'time','signal','owner_decision'"
WORKFLOW_STEP_STATUSES = "'pending','running','succeeded','failed','cancelled'"
WORKFLOW_DECISION_STATES = "'pending','approved','denied','expired','cancelled','consumed'"


class WorkflowExecutionRecord(Base):
    """One owner-scoped durable workflow execution (ADR 0039).

    Deliberately *not* a Run and *not* a Conversation: this row owns ordered steps and
    private application progress, and it references ordinary Runs rather than replacing
    any of them. Waiting is a value of `status` here, which is why a waiting workflow
    occupies no Worker slot, no lease, and no permanent process.
    """

    __tablename__ = "workflow_executions"
    __table_args__ = (
        CheckConstraint(f"status IN ({WORKFLOW_STATUSES})", name="status_value"),
        CheckConstraint(
            f"wait_kind IS NULL OR wait_kind IN ({WORKFLOW_WAIT_KINDS})", name="wait_kind_value"
        ),
        CheckConstraint(
            "(status = 'waiting' AND wait_kind IS NOT NULL AND (wait_kind <> 'time' OR wakeup_at IS NOT NULL))"
            " OR (status <> 'waiting' AND wait_kind IS NULL AND wakeup_at IS NULL)",
            name="wait_shape",
        ),
        CheckConstraint("checkpoint_revision >= 0", name="checkpoint_revision_nonnegative"),
        CheckConstraint("step_count >= 0", name="step_count_nonnegative"),
        CheckConstraint("1 <= max_steps AND max_steps <= 128", name="max_steps_bound"),
        CheckConstraint(
            "1 <= model_call_reservation AND model_call_reservation <= 2048",
            name="model_reservation_bound",
        ),
        CheckConstraint(
            "0 <= tool_call_reservation AND tool_call_reservation <= 2048",
            name="tool_reservation_bound",
        ),
        CheckConstraint(
            "1 <= output_token_reservation AND output_token_reservation <= 2097152",
            name="output_reservation_bound",
        ),
        CheckConstraint(
            "reserved_model_calls >= 0 AND reserved_tool_calls >= 0 AND reserved_output_tokens >= 0",
            name="reserved_nonnegative",
        ),
        CheckConstraint(
            "reserved_model_calls <= model_call_reservation"
            " AND reserved_tool_calls <= tool_call_reservation"
            " AND reserved_output_tokens <= output_token_reservation",
            name="reserved_within_budget",
        ),
        CheckConstraint("1 <= deadline_hours AND deadline_hours <= 168", name="deadline_bound"),
        CheckConstraint(
            "1 <= max_retained_checkpoints AND max_retained_checkpoints <= 128",
            name="retention_bound",
        ),
        CheckConstraint("state_schema_version = 1", name="state_schema_version_value"),
        CheckConstraint(
            "length(submission_key) BETWEEN 1 AND 64 AND submission_key NOT GLOB '*[^a-z0-9_-]*'",
            name="submission_key_bound",
        ),
        CheckConstraint(
            "length(submission_digest) = 64 AND submission_digest NOT GLOB '*[^0-9a-f]*'",
            name="submission_digest_shape",
        ),
        CheckConstraint("length(workflow_kind) BETWEEN 1 AND 64", name="workflow_kind_bound"),
        CheckConstraint("length(agent_key) BETWEEN 1 AND 128", name="agent_key_bound"),
        CheckConstraint(
            "length(agent_definition_version) BETWEEN 1 AND 64", name="definition_version_bound"
        ),
        CheckConstraint(
            "(status IN ('succeeded','failed','cancelled','needs_review') AND finished_at IS NOT NULL)"
            " OR (status NOT IN ('succeeded','failed','cancelled','needs_review') AND finished_at IS NULL)",
            name="terminal_shape",
        ),
        CheckConstraint("deadline_at >= created_at", name="deadline_after_creation"),
        UniqueConstraint("owner_user_id", "submission_key", name="owner_submission_key"),
        # Bounded due-work scan for the Scheduler tick: one index covers "not terminal,
        # not paused, and due" without a full table read.
        Index("ix_workflow_executions_due", "paused", "status", "wakeup_at", "id"),
        Index("ix_workflow_executions_owner_id", "owner_user_id", "id"),
        Index("ix_workflow_executions_agent_instance_id", "agent_instance_id", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    agent_instance_id: Mapped[int] = mapped_column(
        ForeignKey("agent_instances.id", ondelete="RESTRICT"), nullable=False
    )
    workflow_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    state_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    submission_key: Mapped[str] = mapped_column(String(64), nullable=False)
    submission_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    checkpoint_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False, server_default="32")
    model_call_reservation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="256"
    )
    tool_call_reservation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="256"
    )
    output_token_reservation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="262144"
    )
    reserved_model_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    reserved_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    reserved_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    deadline_hours: Mapped[int] = mapped_column(Integer, nullable=False, server_default="24")
    max_retained_checkpoints: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="128"
    )
    deadline_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    wait_kind: Mapped[str | None] = mapped_column(String(24), nullable=True)
    wakeup_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    signal_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_key: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_definition_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(256), nullable=False)
    package_content_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    package_environment_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    effective_config_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_instance_config_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class WorkflowStepRecord(Base):
    """One workflow step: the pinned checkpoint plus the single Run that executes it.

    `run_id` is written inside the very transaction that inserts the Run, so a step is
    never observable in a "created but unlinked" state, and the unique constraints make a
    duplicate continuation impossible even under a retry after an uncertain commit.
    """

    __tablename__ = "workflow_steps"
    __table_args__ = (
        CheckConstraint(f"status IN ({WORKFLOW_STEP_STATUSES})", name="status_value"),
        CheckConstraint("step_number > 0", name="step_number_positive"),
        CheckConstraint("expected_checkpoint_revision >= 0", name="expected_revision_nonnegative"),
        CheckConstraint("state_schema_version = 1", name="state_schema_version_value"),
        CheckConstraint(
            "length(state_digest) = 64 AND state_digest NOT GLOB '*[^0-9a-f]*'",
            name="state_digest_shape",
        ),
        CheckConstraint("length(CAST(state_json AS BLOB)) <= 65536", name="state_byte_bound"),
        CheckConstraint("summary IS NULL OR length(summary) <= 512", name="summary_bound"),
        CheckConstraint(
            "(status IN ('succeeded','failed','cancelled') AND finished_at IS NOT NULL)"
            " OR (status NOT IN ('succeeded','failed','cancelled') AND finished_at IS NULL)",
            name="terminal_shape",
        ),
        UniqueConstraint("workflow_id", "step_number", name="workflow_step_number"),
        UniqueConstraint("workflow_id", "run_id", name="workflow_run"),
        UniqueConstraint("run_id", name="single_run_per_step"),
        Index("ix_workflow_steps_workflow_id", "workflow_id", "step_number"),
        Index("ix_workflow_steps_run_id", "run_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="RESTRICT"), nullable=False
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"), nullable=True
    )
    expected_checkpoint_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    state_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    state_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class WorkflowCheckpointRecord(Base):
    """One immutable committed revision of application state. Append-only evidence."""

    __tablename__ = "workflow_checkpoints"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="revision_nonnegative"),
        CheckConstraint("step_number > 0", name="step_number_positive"),
        CheckConstraint("state_schema_version = 1", name="state_schema_version_value"),
        CheckConstraint(
            "length(state_digest) = 64 AND state_digest NOT GLOB '*[^0-9a-f]*'",
            name="state_digest_shape",
        ),
        CheckConstraint("length(CAST(state_json AS BLOB)) <= 65536", name="state_byte_bound"),
        UniqueConstraint("workflow_id", "revision", name="workflow_revision"),
        Index("ix_workflow_checkpoints_workflow_id", "workflow_id", "revision"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="RESTRICT"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    state_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    state_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class WorkflowSignalRecord(Base):
    """One owner wakeup delivery. The unique key makes a duplicate a replay, not a re-advance."""

    __tablename__ = "workflow_signals"
    __table_args__ = (
        CheckConstraint("expected_revision >= 0", name="expected_revision_nonnegative"),
        CheckConstraint(
            "length(payload_digest) = 64 AND payload_digest NOT GLOB '*[^0-9a-f]*'",
            name="payload_digest_shape",
        ),
        CheckConstraint("length(CAST(signal_json AS BLOB)) <= 8192", name="signal_byte_bound"),
        CheckConstraint("length(signal_key) BETWEEN 1 AND 64", name="signal_key_bound"),
        CheckConstraint("accepted_at IS NULL OR accepted_at >= received_at", name="accepted_order"),
        UniqueConstraint("workflow_id", "signal_key", name="workflow_signal_key"),
        Index("ix_workflow_signals_workflow_id", "workflow_id", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="RESTRICT"), nullable=False
    )
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    signal_key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    signal_json: Mapped[str] = mapped_column(Text, nullable=False)
    expected_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class WorkflowDecisionRecord(Base):
    """One durable owner decision about proposed work (ADR 0039).

    **Not** a Stage-H `action_approvals` row: this authorizes *proposing* the next step and
    grants nothing. Every dispatch still requires a fresh live-Attempt approval.
    """

    __tablename__ = "workflow_decisions"
    __table_args__ = (
        CheckConstraint(f"state IN ({WORKFLOW_DECISION_STATES})", name="state_value"),
        CheckConstraint("checkpoint_revision >= 0", name="checkpoint_revision_nonnegative"),
        CheckConstraint(
            "length(action_fingerprint) = 64 AND action_fingerprint NOT GLOB '*[^0-9a-f]*'",
            name="fingerprint_shape",
        ),
        CheckConstraint(
            "length(arguments_digest) = 64 AND arguments_digest NOT GLOB '*[^0-9a-f]*'",
            name="arguments_digest_shape",
        ),
        CheckConstraint("length(CAST(preview_json AS BLOB)) <= 65536", name="preview_byte_bound"),
        CheckConstraint("expires_at > requested_at", name="expiry_after_request"),
        CheckConstraint(
            "(state = 'pending' AND decided_at IS NULL AND consumed_at IS NULL)"
            " OR (state = 'approved' AND decided_at IS NOT NULL AND consumed_at IS NULL)"
            " OR (state = 'consumed' AND decided_at IS NOT NULL AND consumed_at IS NOT NULL)"
            " OR (state IN ('denied','expired','cancelled') AND consumed_at IS NULL)",
            name="lifecycle_shape",
        ),
        UniqueConstraint(
            "workflow_id",
            "checkpoint_revision",
            "tool_definition_id",
            name="workflow_revision_tool",
        ),
        Index("ix_workflow_decisions_owner_state", "owner_user_id", "state", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_executions.id", ondelete="RESTRICT"), nullable=False
    )
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    checkpoint_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_definition_id: Mapped[int] = mapped_column(
        ForeignKey("tool_definitions.id"), nullable=False
    )
    upstream_name: Mapped[str] = mapped_column(String(128), nullable=False)
    action_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_json: Mapped[str] = mapped_column(Text, nullable=False)
    package_content_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    effective_config_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


# H5: local publisher trust, keyed by the Stage-G signer fingerprint. This is mechanism
# evidence turned into an explicit local decision; hosted Marketplace state cannot flip it.

TRUST_STATES = "'trusted','untrusted','revoked'"


class PublisherTrustRecord(Base):
    __tablename__ = "publisher_trust"
    __table_args__ = (
        CheckConstraint(f"state IN ({TRUST_STATES})", name="state_value"),
        CheckConstraint(
            "length(signer_fingerprint) = 64 AND signer_fingerprint NOT GLOB '*[^0-9a-f]*'",
            name="fingerprint_shape",
        ),
        CheckConstraint("length(reason) <= 512", name="reason_bound"),
        UniqueConstraint("signer_fingerprint", name="signer_identity"),
    )

    signer_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
