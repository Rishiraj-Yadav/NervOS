"""Typed process configuration for the NervOS Scheduler.

This is the smallest settings class in the repository, and that is the point: a process whose whole
job is to compare a stored instant against `now` needs no provider credential, no concurrency
knob, no origin allow-list and no credential alias. An exposed setting needs a demonstrated
operator requirement, and this process has none beyond where the database is and how loudly to log.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class SchedulerSettings(BaseSettings):
    """Validated Scheduler configuration loaded only from the process environment."""

    model_config = SettingsConfigDict(
        env_prefix="NERVOS_",
        env_file=None,
        frozen=True,
        validate_default=True,
    )

    environment: Environment = "development"
    database_path: Path = Path("~/.nervos/nervos.db")
    log_level: LogLevel = "INFO"
    # Test-only readiness marker. Production never sets it, so production never writes a file.
    scheduler_ready_file: Path | None = None

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

    def require_scheduler_ready_file(self) -> Path | None:
        """Return the explicitly configured readiness marker path, if any."""
        if self.scheduler_ready_file is None:
            return None
        return self.scheduler_ready_file.expanduser().resolve(strict=False)


@lru_cache(maxsize=1)
def get_scheduler_settings() -> SchedulerSettings:
    """Return the validated Scheduler settings for normal composition."""
    return SchedulerSettings()
