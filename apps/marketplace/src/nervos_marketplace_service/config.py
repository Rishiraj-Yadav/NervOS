"""Process-only hosted settings. Sensitive fields cannot be dumped or represented."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class MarketplaceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NERVOS_MARKETPLACE_", env_file=None, frozen=True, hide_input_in_errors=True
    )
    environment: Literal["development", "test", "production"]
    database_dsn: SecretStr = Field(repr=False, exclude=True)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    s3_endpoint_url: str = Field(repr=False, exclude=True)
    s3_region: str
    s3_bucket: str = Field(min_length=3, max_length=63, repr=False, exclude=True)
    s3_access_key_id: SecretStr = Field(repr=False, exclude=True)
    s3_secret_access_key: SecretStr = Field(repr=False, exclude=True)
    s3_session_token: SecretStr | None = Field(default=None, repr=False, exclude=True)
    s3_addressing_style: Literal["path", "virtual"] = "path"
    artifact_temp_directory: Path = Field(repr=False, exclude=True)
    db_pool_size: int = Field(default=5, ge=1, le=20)
    db_max_overflow: int = Field(default=5, ge=0, le=20)
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=30)
    db_statement_timeout_ms: int = Field(default=3000, ge=100, le=30000)
    db_transaction_timeout_ms: int = Field(default=10000, ge=1000, le=60000)
    s3_connect_timeout_seconds: int = Field(default=3, ge=1, le=10)
    s3_read_timeout_seconds: int = Field(default=10, ge=1, le=30)
    artifact_operation_timeout_seconds: int = Field(default=120, ge=1, le=300)
    max_concurrent_artifact_reads: int = Field(default=4, ge=1, le=16)

    @model_validator(mode="after")
    def validate_transports(self) -> "MarketplaceSettings":
        try:
            url = make_url(self.database_dsn.get_secret_value())
            endpoint = urlsplit(self.s3_endpoint_url)
            if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
                raise ValueError
            if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
                raise ValueError
            if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                raise ValueError
            if endpoint.path not in {"", "/"}:
                raise ValueError
            if endpoint.scheme == "http" and (
                self.environment == "production"
                or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ValueError
            if self.environment == "production" and url.query.get("sslmode") not in {
                "verify-full",
                "verify-ca",
            }:
                raise ValueError
            if any(
                not secret.get_secret_value().strip()
                for secret in (self.database_dsn, self.s3_access_key_id, self.s3_secret_access_key)
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Invalid hosted transport configuration") from None
        return self
