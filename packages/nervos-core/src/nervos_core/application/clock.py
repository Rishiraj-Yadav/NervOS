"""Application clock boundary."""

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Return the current timezone-aware UTC instant."""

    def __call__(self) -> datetime: ...


def require_utc(value: datetime) -> datetime:
    """Validate and normalize an application clock value."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("application clock must return an aware datetime")
    return value.astimezone(UTC)
