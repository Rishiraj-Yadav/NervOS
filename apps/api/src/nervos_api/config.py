"""Typed process configuration for the NervOS API."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

# Exact external variable name read for the first provider credential. The `NERVOS_`
# prefix is deliberately bypassed so the operator's standard process variable is used.
ANTHROPIC_API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
OPENAI_API_KEY_VARIABLE = "OPENAI_API_KEY"


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
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    def without_provider_credentials(self) -> Settings:
        """Return an equal configuration holding neither provider credential.

        A credential is read exactly once, locally, to build its provider client. Every
        object that outlives composition - middleware, ``app.state``, and any later
        consumer - receives this copy instead, so no secret-bearing settings object stays
        reachable from the running application.
        """
        return self.model_copy(update={"anthropic_api_key": None, "openai_api_key": None})

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
