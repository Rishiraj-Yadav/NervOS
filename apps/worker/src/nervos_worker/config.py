"""Typed process configuration for the NervOS Worker.

This is the only settings class in NervOS that reads a provider credential. The control plane
accepts Runs without one, because execution is a Worker capability; the Worker holds the keys
exclusively and never persists, logs, or exposes them.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

# Exact external variable names read for provider credentials. The `NERVOS_` prefix is
# deliberately bypassed so the operator's standard process variables are used.
ANTHROPIC_API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
OPENAI_API_KEY_VARIABLE = "OPENAI_API_KEY"


class WorkerSettings(BaseSettings):
    """Validated Worker configuration loaded only from the process environment."""

    model_config = SettingsConfigDict(
        env_prefix="NERVOS_",
        env_file=None,
        frozen=True,
        validate_default=True,
    )

    environment: Environment = "development"
    database_path: Path = Path("~/.nervos/nervos.db")
    log_level: LogLevel = "INFO"
    worker_concurrency: int = Field(default=1, ge=1, le=16)
    max_active_jobs: int = Field(default=4, ge=1, le=16)
    # Test-only readiness marker. Production never sets it, so production never writes a file.
    worker_ready_file: Path | None = None
    anthropic_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=ANTHROPIC_API_KEY_VARIABLE,
        repr=False,
        exclude=True,
    )
    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=OPENAI_API_KEY_VARIABLE,
        repr=False,
        exclude=True,
    )

    @field_validator("anthropic_api_key", "openai_api_key", mode="before")
    @classmethod
    def normalize_provider_api_key(cls, value: object) -> object:
        """Treat an absent, empty, or whitespace-only credential as unconfigured."""
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("database_path", mode="before")
    @classmethod
    def reject_blank_database_path(cls, value: object) -> object:
        """Reject an empty environment value before Path coercion."""
        if isinstance(value, str) and not value.strip():
            raise ValueError("database path must not be blank")
        return value

    @field_validator("database_path")
    @classmethod
    def normalize_database_path(cls, value: Path) -> Path:
        """Expand and resolve a database path without creating it."""
        path = value.expanduser().resolve(strict=False)
        if path.is_dir():
            raise ValueError("database path must identify a file")
        return path

    def require_worker_ready_file(self) -> Path | None:
        """Return the explicitly configured readiness marker path, if any."""
        if self.worker_ready_file is None:
            return None
        return self.worker_ready_file.expanduser().resolve(strict=False)


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    """Return the validated Worker settings for normal composition."""
    return WorkerSettings()
