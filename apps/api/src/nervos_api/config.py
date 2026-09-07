"""Typed process configuration for the NervOS API."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    """Validated API configuration loaded only from process environment."""

    model_config = SettingsConfigDict(
        env_prefix="NERVOS_",
        env_file=None,
        frozen=True,
        validate_default=True,
    )

    environment: Environment = "development"
    database_path: Path = Path("~/.nervos/nervos.db")
    app_origin: str = "http://localhost:5173"
    log_level: LogLevel = "INFO"

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

    @field_validator("app_origin")
    @classmethod
    def validate_app_origin(cls, value: str) -> str:
        """Require one exact HTTP(S) origin without URL suffixes."""
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError as error:
            raise ValueError("app origin must contain a valid port") from error

        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("app origin must be an HTTP(S) origin with a host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("app origin must not contain credentials")
        if parsed.path or parsed.query or parsed.fragment:
            raise ValueError("app origin must not contain a path, query, or fragment")
        if "*" in parsed.hostname:
            raise ValueError("app origin must not contain a wildcard")
        return value

    @model_validator(mode="after")
    def require_production_https(self) -> Self:
        """Reject insecure external origins in production."""
        if self.environment == "production" and urlsplit(self.app_origin).scheme != "https":
            raise ValueError("production app origin must use HTTPS")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the validated process settings for normal composition."""
    return Settings()
