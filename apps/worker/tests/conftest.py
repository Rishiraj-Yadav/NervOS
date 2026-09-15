"""Shared Worker fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from nervos_worker.config import WorkerSettings, get_worker_settings


@pytest.fixture(autouse=True)
def clear_worker_settings_cache() -> Iterator[None]:
    """Prevent cached process settings leaking between tests."""
    get_worker_settings.cache_clear()
    yield
    get_worker_settings.cache_clear()


@pytest.fixture
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkerSettings:
    """Return Worker settings pointing at a disposable database."""
    database = (tmp_path / "worker.db").resolve()
    monkeypatch.setenv("NERVOS_ENVIRONMENT", "test")
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(database))
    return WorkerSettings()
