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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class RunEventRecord(Base):
    """Append-only safe lifecycle fact, sequenced within one Run."""

    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence"),
        CheckConstraint("sequence > 0", name="sequence_positive"),
        CheckConstraint(
            "event_type IN ('run.created','run.queued','attempt.claimed','attempt.started','attempt.failed','attempt.expired','retry.scheduled','cancellation.requested','run.cancelled','run.succeeded','run.failed','recovery.pre_start','recovery.ambiguous')",
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
