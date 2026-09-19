"""Scheduler settings: defaults, bounds, and the absence of anything it does not need.

The settings surface is small on purpose, and the last test here is what keeps it that way: a
process whose whole job is comparing a stored instant against `now` has no business reading a
provider credential, an origin allow-list or a concurrency knob.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nervos_scheduler.config import SchedulerSettings
from pydantic import ValidationError


def test_defaults_match_the_approved_configuration() -> None:
    settings = SchedulerSettings(database_path=Path("~/nervos.db"))

    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.scheduler_ready_file is None


def test_the_settings_surface_carries_no_provider_or_origin_configuration() -> None:
    """The scheduler holds no credential, dials no origin, and configures no concurrency."""
    fields = set(SchedulerSettings.model_fields)
    assert fields == {
        "environment",
        "database_path",
        "log_level",
        "scheduler_ready_file",
    }
    serialized = SchedulerSettings(database_path=Path("~/nervos.db")).model_dump()
    assert "api_key" not in repr(serialized).lower()
    assert "origin" not in repr(serialized).lower()


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_database_path_is_refused(value: str) -> None:
    with pytest.raises(ValidationError):
        SchedulerSettings(database_path=value)  # type: ignore[arg-type]


def test_a_directory_is_not_a_database(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        SchedulerSettings(database_path=tmp_path)


def test_the_database_path_is_expanded_and_resolved(tmp_path: Path) -> None:
    settings = SchedulerSettings(database_path=tmp_path / "nested" / ".." / "nervos.db")
    assert settings.database_path.is_absolute()
    assert ".." not in str(settings.database_path)


def test_an_invalid_log_level_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        SchedulerSettings(database_path=tmp_path / "nervos.db", log_level="LOUD")  # type: ignore[arg-type]


def test_settings_are_immutable(tmp_path: Path) -> None:
    settings = SchedulerSettings(database_path=tmp_path / "nervos.db")
    with pytest.raises(ValidationError):
        settings.log_level = "DEBUG"  # type: ignore[misc]


def test_the_ready_file_is_absent_unless_explicitly_configured(tmp_path: Path) -> None:
    settings = SchedulerSettings(database_path=tmp_path / "nervos.db")
    assert settings.require_scheduler_ready_file() is None
    configured = SchedulerSettings(
        database_path=tmp_path / "nervos.db", scheduler_ready_file=tmp_path / "ready"
    )
    assert configured.require_scheduler_ready_file() == (tmp_path / "ready").resolve()
