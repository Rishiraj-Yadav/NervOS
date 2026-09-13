"""Shared pytest configuration for the NervOS workspace."""

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Run `@pytest.mark.anyio` tests on asyncio only, matching production execution."""
    return "asyncio"
