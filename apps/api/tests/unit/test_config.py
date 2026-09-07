"""Unit tests for process-environment configuration."""

from pathlib import Path

import pytest
from nervos_api.config import Settings
from pydantic import ValidationError

VARIABLES = (
    "NERVOS_ENVIRONMENT",
    "NERVOS_DATABASE_PATH",
    "NERVOS_APP_ORIGIN",
    "NERVOS_LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def clear_nervos_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)


def test_defaults_are_validated_without_creating_database() -> None:
    settings = Settings()

    assert settings.environment == "development"
    assert settings.database_path == (Path.home() / ".nervos" / "nervos.db").resolve()
    assert settings.app_origin == "http://localhost:5173"
    assert settings.log_level == "INFO"
    assert Settings.model_config.get("env_file") is None


def test_process_environment_populates_all_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "configured.db"
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("NERVOS_APP_ORIGIN", "https://localhost:8443")
    monkeypatch.setenv("NERVOS_LOG_LEVEL", "DEBUG")

    settings = Settings()

    assert settings.environment == "test"
    assert settings.database_path == database_path.resolve()
    assert settings.app_origin == "https://localhost:8443"
    assert settings.log_level == "DEBUG"
    assert not database_path.exists()


@pytest.mark.parametrize("value", ["", "staging", "DEVELOPMENT"])
def test_invalid_environment_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(environment=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["", "info", "TRACE", "WARN"])
def test_invalid_log_level_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(log_level=value)  # type: ignore[arg-type]


def test_database_path_expands_user_and_resolves_relative_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    relative = Settings(database_path=Path("data/nervos.db"))
    expanded = Settings(database_path=Path("~/custom-nervos.db"))

    assert relative.database_path == (tmp_path / "data" / "nervos.db").resolve()
    assert expanded.database_path == (Path.home() / "custom-nervos.db").resolve()
    assert not (tmp_path / "data").exists()


def test_blank_or_existing_directory_database_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        Settings.model_validate({"database_path": ""})
    with pytest.raises(ValidationError, match="identify a file"):
        Settings(database_path=tmp_path)


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://localhost:5173",
        "http://",
        "http://*.example.test",
        "http://user@example.test",
        "http://example.test/",
        "http://example.test/path",
        "http://example.test?query=yes",
        "http://example.test#fragment",
        "http://example.test:not-a-port",
    ],
)
def test_invalid_origins_are_rejected(origin: str) -> None:
    with pytest.raises(ValidationError):
        Settings(app_origin=origin)


@pytest.mark.parametrize("origin", ["http://localhost:5173", "https://example.test:8443"])
def test_valid_development_origins_are_accepted(origin: str) -> None:
    assert Settings(app_origin=origin).app_origin == origin


def test_production_requires_https_origin() -> None:
    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings(environment="production", app_origin="http://example.test")

    assert (
        Settings(environment="production", app_origin="https://example.test").app_origin
        == "https://example.test"
    )
