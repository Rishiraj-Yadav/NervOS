"""Shared fixtures for the ``nervos-mcp`` suite.

The workspace ``conftest.py`` already pins ``@pytest.mark.anyio`` tests to asyncio, matching
production execution. Nothing global is added here: each test builds the one double or row it needs,
so no fixture can leak state between tests or into the developer's environment.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from nervos_core.application.mcp_connections import (
    ConnectionStatus,
    ConnectionTransport,
    McpConnectionRow,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def connection_row(
    *,
    connection_id: int = 1,
    transport: ConnectionTransport = ConnectionTransport.HTTP,
    endpoint: str | None = "https://docs.example/mcp",
    server_key: str | None = None,
    credential_ref: str | None = None,
) -> McpConnectionRow:
    """Build one durable connection row with only the fields a test cares about varying."""
    return McpConnectionRow(
        connection_id=connection_id,
        owner_user_id=1,
        display_name="Fake Docs",
        transport=transport,
        endpoint=endpoint,
        server_key=server_key,
        credential_ref=credential_ref,
        enabled=True,
        catalog_status=ConnectionStatus.NEEDS_REFRESH,
        last_discovery_at=None,
        last_error_code=None,
        last_error_message=None,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def make_connection() -> Callable[..., McpConnectionRow]:
    """Return the row factory, so a test names only the field it is exercising."""
    return connection_row
