"""Shared Scheduler fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from nervos_scheduler.config import SchedulerSettings, get_scheduler_settings


@pytest.fixture(autouse=True)
def clear_scheduler_settings_cache() -> Iterator[None]:
    """Prevent cached process settings leaking between tests."""
    get_scheduler_settings.cache_clear()
    yield
    get_scheduler_settings.cache_clear()


@pytest.fixture
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SchedulerSettings:
    """Return Scheduler settings pointing at a disposable database."""
    database = (tmp_path / "scheduler.db").resolve()
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(database))
    monkeypatch.delenv("NERVOS_SCHEDULER_READY_FILE", raising=False)
    return SchedulerSettings()
