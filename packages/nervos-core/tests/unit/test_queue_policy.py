"""C6 queue policy: the constants every Worker shares, and their validation.

These limits are deliberately not settings. A claim compares a database-wide count against its
limit, so two Workers holding different limits would let the fleet run above the intended bound;
a shared constant cannot diverge. This suite pins the defaults, the bound, and the rejection of
an unusable limit.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from nervos_core.application import queue_policy
from nervos_core.application.queue_policy import (
    GLOBAL_ACTIVE_LIMIT,
    MAX_ACTIVE_LIMIT,
    MIN_ACTIVE_LIMIT,
    PER_AGENT_ACTIVE_LIMIT,
    PER_PROVIDER_ACTIVE_LIMIT,
    PRODUCTION_QUEUE_POLICY,
    QueuePolicy,
)

LIMIT_FIELDS = ("global_active_limit", "per_agent_active_limit", "per_provider_active_limit")


def test_the_production_policy_defaults_to_the_shared_constants() -> None:
    assert PRODUCTION_QUEUE_POLICY.global_active_limit == GLOBAL_ACTIVE_LIMIT
    assert PRODUCTION_QUEUE_POLICY.per_agent_active_limit == PER_AGENT_ACTIVE_LIMIT
    assert PRODUCTION_QUEUE_POLICY.per_provider_active_limit == PER_PROVIDER_ACTIVE_LIMIT


def test_every_default_stays_inside_the_accepted_bound() -> None:
    for limit in (GLOBAL_ACTIVE_LIMIT, PER_AGENT_ACTIVE_LIMIT, PER_PROVIDER_ACTIVE_LIMIT):
        assert MIN_ACTIVE_LIMIT <= limit <= MAX_ACTIVE_LIMIT


def test_a_dimension_defaults_to_the_global_limit() -> None:
    """A dimension capped below the global limit would refuse work C2 admitted; above it, the
    dimension would never bind. The defaults agree so C6 adds the dimensions without changing
    what a deployment admits or executes out of the box."""
    assert PER_AGENT_ACTIVE_LIMIT == GLOBAL_ACTIVE_LIMIT
    assert PER_PROVIDER_ACTIVE_LIMIT == GLOBAL_ACTIVE_LIMIT


def test_the_policy_is_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        PRODUCTION_QUEUE_POLICY.global_active_limit = 99  # type: ignore[misc]


@pytest.mark.parametrize("field", LIMIT_FIELDS)
@pytest.mark.parametrize("value", [0, -1, MAX_ACTIVE_LIMIT + 1])
def test_an_unusable_limit_is_refused_by_name(field: str, value: int) -> None:
    with pytest.raises(ValueError, match=field):
        QueuePolicy(**{field: value})


@pytest.mark.parametrize("value", [MIN_ACTIVE_LIMIT, MAX_ACTIVE_LIMIT])
def test_the_bound_is_inclusive(value: int) -> None:
    policy = QueuePolicy(
        global_active_limit=value,
        per_agent_active_limit=value,
        per_provider_active_limit=value,
    )
    assert policy.global_active_limit == value


def test_provider_limits_are_generic_and_name_no_provider() -> None:
    """Per-provider means one generic limit applied to each provider identity independently."""
    source = Path(queue_policy.__file__).read_text(encoding="utf-8").lower()
    for forbidden in ("anthropic", "openai"):
        assert forbidden not in source, forbidden


def test_the_policy_holds_no_persisted_or_mutable_state() -> None:
    """Nothing here can drift, because nothing here is stored, counted, or incremented."""
    source = Path(queue_policy.__file__).read_text(encoding="utf-8").lower()
    for forbidden in ("active_count", "pending_count", "increment", "sqlalchemy", "insert("):
        assert forbidden not in source, forbidden
