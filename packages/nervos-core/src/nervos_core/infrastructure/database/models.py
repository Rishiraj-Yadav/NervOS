# ruff: noqa: E501
"""SQLAlchemy persistence records for the current application schema."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from nervos_core.infrastructure.database.base import Base
from nervos_core.infrastructure.database.types import UTCDateTime

NERVOS_BLANK_TEXT_SQL_CHARS = (
    "char(9,10,11,12,13,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,"
    "8200,8201,8202,8232,8233,8239,8287,12288)"
)


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


class RunRecord(Base):
    """Persistence record for one immutable-snapshot Run."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint("agent_instance_id > 0", name="agent_instance_positive"),
        CheckConstraint(
            "status IN ('created','running','succeeded','failed')", name="status_value"
        ),
        CheckConstraint(
            "input_max_bytes > 0 AND input_max_code_points > 0 AND output_max_bytes > 0 AND output_max_code_points > 0 AND provider_timeout_ms > 0 AND max_output_tokens > 0 AND max_model_calls > 0",
            name="limits_positive",
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
            "finished_at IS NULL OR (started_at IS NOT NULL AND finished_at >= started_at)",
            name="finished_order",
        ),
        CheckConstraint(
            "(status='created' AND started_at IS NULL AND finished_at IS NULL AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NULL AND error_message IS NULL AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL AND elapsed_ms IS NULL) OR (status='running' AND started_at IS NOT NULL AND finished_at IS NULL AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NULL AND error_message IS NULL AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL AND elapsed_ms IS NULL) OR (status='succeeded' AND started_at IS NOT NULL AND finished_at IS NOT NULL AND output_text IS NOT NULL AND length(trim(output_text, char(9,10,11,12,13,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288))) > 0 AND error_code IS NULL AND error_message IS NULL AND elapsed_ms IS NOT NULL) OR (status='failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL AND output_text IS NULL AND finish_reason IS NULL AND error_code IS NOT NULL AND error_message IS NOT NULL AND length(trim(error_message, char(9,10,11,12,13,32,133,160,5760,8192,8193,8194,8195,8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288))) > 0 AND elapsed_ms IS NOT NULL)",
            name="lifecycle_shape",
        ),
        Index("ix_runs_agent_instance_id_id", "agent_instance_id", "id"),
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
