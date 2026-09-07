"""Shared fixtures for API tests."""

from collections.abc import Iterator

import pytest
from nervos_api.config import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Iterator[None]:
    """Prevent process-setting cache state from leaking between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
