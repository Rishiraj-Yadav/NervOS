"""Unit tests for UTCDateTime normalization."""

from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest
from nervos_core.infrastructure.database.types import UTCDateTime
from sqlalchemy.dialects import sqlite


class IneffectiveTimezone(tzinfo):
    """Timezone whose offset is intentionally undefined."""

    def utcoffset(self, value: datetime | None) -> None:
        del value
        return None

    def dst(self, value: datetime | None) -> None:
        del value
        return None

    def tzname(self, value: datetime | None) -> str:
        del value
        return "ineffective"


def test_utc_datetime_passes_none_through() -> None:
    type_ = UTCDateTime()
    dialect = sqlite.dialect()

    assert type_.process_bind_param(None, dialect) is None
    assert type_.process_result_value(None, dialect) is None


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 1, 2, 3, 4, 5),
        datetime(2026, 1, 2, 3, 4, 5, tzinfo=IneffectiveTimezone()),
    ],
)
def test_utc_datetime_rejects_values_without_effective_offset(value: datetime) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        UTCDateTime().process_bind_param(value, sqlite.dialect())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            datetime(2026, 1, 2, 8, 34, 5, tzinfo=timezone(timedelta(hours=5, minutes=30))),
            datetime(2026, 1, 2, 3, 4, 5),
        ),
        (
            datetime(2026, 1, 1, 19, 4, 5, tzinfo=timezone(timedelta(hours=-8))),
            datetime(2026, 1, 2, 3, 4, 5),
        ),
    ],
)
def test_utc_datetime_normalizes_aware_values(value: datetime, expected: datetime) -> None:
    assert UTCDateTime().process_bind_param(value, sqlite.dialect()) == expected


def test_utc_datetime_restores_exact_utc_timezone() -> None:
    stored = datetime(2026, 1, 2, 3, 4, 5)

    restored = UTCDateTime().process_result_value(stored, sqlite.dialect())

    assert restored == stored.replace(tzinfo=UTC)
    assert restored is not None
    assert restored.tzinfo is UTC
