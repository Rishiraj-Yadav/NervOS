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
    publisher_enabled: bool = False
    public_origin: str | None = None
    writer_database_dsn: SecretStr | None = Field(default=None, repr=False, exclude=True)
    verifier_database_dsn: SecretStr | None = Field(default=None, repr=False, exclude=True)
    quarantine_access_key_id: SecretStr | None = Field(default=None, repr=False, exclude=True)
    quarantine_secret_access_key: SecretStr | None = Field(default=None, repr=False, exclude=True)
    finalizer_access_key_id: SecretStr | None = Field(default=None, repr=False, exclude=True)
    finalizer_secret_access_key: SecretStr | None = Field(default=None, repr=False, exclude=True)
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = Field(default=None, repr=False, exclude=True)
    auth_transaction_encryption_key: SecretStr | None = Field(
        default=None, repr=False, exclude=True
    )
    oidc_algorithms: tuple[Literal["RS256", "ES256"], ...] = ("RS256",)
    accepted_acr_values: tuple[str, ...] = ()
    required_amr_values: tuple[str, ...] = ()
    recent_auth_max_age_seconds: int = Field(default=300, ge=30, le=300)
    session_absolute_seconds: int = Field(default=28800, ge=300, le=28800)
    session_idle_seconds: int = Field(default=1800, ge=60, le=1800)
    cli_token_seconds: int = Field(default=900, ge=60, le=900)
    upload_timeout_seconds: int = Field(default=300, ge=1, le=300)
    upload_idle_seconds: int = Field(default=15, ge=1, le=15)
    active_uploads_per_account: int = Field(default=2, ge=1, le=2)
    active_uploads_per_publisher: int = Field(default=4, ge=1, le=4)
    daily_upload_bytes: int = Field(default=2 * 1024**3, ge=268435456, le=2 * 1024**3)
    projects_per_publisher: int = Field(default=100, ge=1, le=100)
    challenges_per_minute: int = Field(default=10, ge=1, le=10)
    verifier_memory_bytes: int = Field(default=2 * 1024**3, ge=64 * 1024**2, le=2 * 1024**3)
    verifier_wall_seconds: int = Field(default=120, ge=1, le=120)
    verifier_cpu_seconds: int = Field(default=60, ge=1, le=60)
    verifier_lease_seconds: int = Field(default=60, ge=15, le=60)
    verifier_heartbeat_seconds: int = Field(default=10, ge=1, le=10)

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

    @model_validator(mode="after")
    def validate_publisher(self) -> "MarketplaceSettings":
        if not self.publisher_enabled:
            return self
        try:
            import base64

            if not all(
                (
                    self.public_origin,
                    self.oidc_issuer,
                    self.oidc_client_id,
                    self.writer_database_dsn,
                    self.quarantine_access_key_id,
                    self.quarantine_secret_access_key,
                    self.finalizer_access_key_id,
                    self.finalizer_secret_access_key,
                    self.auth_transaction_encryption_key,
                )
            ):
                raise ValueError
            origin = urlsplit(self.public_origin or "")
            issuer = urlsplit(self.oidc_issuer or "")
            if origin.scheme != "https" or not origin.hostname or origin.path not in {"", "/"}:
                raise ValueError
            for endpoint in (origin, issuer):
                if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                    raise ValueError
                if endpoint.scheme != "https" and not (
                    self.environment in {"development", "test"}
                    and endpoint.scheme == "http"
                    and endpoint.hostname in {"127.0.0.1", "localhost", "::1"}
                ):
                    raise ValueError
            assert self.auth_transaction_encryption_key is not None
            if (
                len(
                    base64.b64decode(
                        self.auth_transaction_encryption_key.get_secret_value(), validate=True
                    )
                )
                != 32
            ):
                raise ValueError
            for dsn in (self.writer_database_dsn, self.verifier_database_dsn):
                if dsn is not None:
                    url = make_url(dsn.get_secret_value())
                    if url.drivername != "postgresql+psycopg" or not url.host or not url.database:
                        raise ValueError
                    if self.environment == "production" and url.query.get("sslmode") not in {
                        "verify-ca",
                        "verify-full",
                    }:
                        raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Invalid publisher configuration") from None
        return self
