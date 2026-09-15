"""Worker settings: bounds, defaults, and the credential boundary."""

from __future__ import annotations

from pathlib import Path

import pytest
from nervos_worker.config import WorkerSettings
from pydantic import ValidationError

CREDENTIAL_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "NERVOS_ANTHROPIC_API_KEY",
    "NERVOS_OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def clear_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in CREDENTIAL_VARIABLES:
        monkeypatch.delenv(variable, raising=False)


def test_defaults_match_the_approved_configuration(tmp_path: Path) -> None:
    monkeypatch_database = str(tmp_path / "nervos.db")
    settings = WorkerSettings(database_path=Path(monkeypatch_database))

    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.worker_concurrency == 1
    assert settings.max_active_jobs == 4
    assert settings.worker_ready_file is None
    assert settings.anthropic_api_key is None and settings.openai_api_key is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("worker_concurrency", 0),
        ("worker_concurrency", 17),
        ("max_active_jobs", 0),
        ("max_active_jobs", 17),
    ],
)
def test_execution_limits_are_bounded(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        WorkerSettings.model_validate({field: value})


def test_credentials_read_the_exact_external_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai")
    monkeypatch.setenv("NERVOS_ANTHROPIC_API_KEY", "must-not-be-read")

    settings = WorkerSettings()

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "synthetic-anthropic"
    assert settings.openai_api_key is not None
    assert settings.openai_api_key.get_secret_value() == "synthetic-openai"


@pytest.mark.parametrize("blank", ["", "   ", "\t\r\n"])
def test_a_blank_credential_is_treated_as_unconfigured(
    blank: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", blank)
    monkeypatch.setenv("OPENAI_API_KEY", blank)

    settings = WorkerSettings()

    assert settings.anthropic_api_key is None
    assert settings.openai_api_key is None


def test_no_credential_appears_in_repr_or_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC-WORKER-CREDENTIAL")
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC-SECOND-WORKER-CREDENTIAL")

    settings = WorkerSettings()

    for rendered in (repr(settings), str(settings.model_dump()), str(settings)):
        assert "SYNTHETIC-WORKER-CREDENTIAL" not in rendered
        assert "SYNTHETIC-SECOND-WORKER-CREDENTIAL" not in rendered
    assert "anthropic_api_key" not in settings.model_dump()
    assert "openai_api_key" not in settings.model_dump()


def test_database_path_is_expanded_and_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    settings = WorkerSettings(database_path=Path("data/worker.db"))

    assert settings.database_path == (tmp_path / "data" / "worker.db").resolve()
    assert not (tmp_path / "data").exists()


def test_blank_and_directory_database_paths_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        WorkerSettings.model_validate({"database_path": ""})
    with pytest.raises(ValidationError, match="identify a file"):
        WorkerSettings(database_path=tmp_path)


def test_a_ready_marker_is_written_only_when_configured(tmp_path: Path) -> None:
    assert WorkerSettings(database_path=tmp_path / "db").require_worker_ready_file() is None

    configured = WorkerSettings(
        database_path=tmp_path / "db", worker_ready_file=tmp_path / "ready.txt"
    )
    assert configured.require_worker_ready_file() == (tmp_path / "ready.txt").resolve()
