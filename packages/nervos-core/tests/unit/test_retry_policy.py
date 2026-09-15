"""C4 retry policy: a pure, deterministic, bounded backoff decision."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from nervos_core.application.retry_policy import (
    PRODUCTION_RETRY_POLICY,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
    RETRY_MAX_ORDINAL,
    CappedExponentialRetry,
    RetryPolicy,
    retry_delay,
    retry_due_at,
)

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def test_the_production_delay_sequence_is_exactly_1_2_4_then_flat() -> None:
    assert [retry_delay(ordinal) for ordinal in range(1, 7)] == [
        timedelta(seconds=1),
        timedelta(seconds=2),
        timedelta(seconds=4),
        timedelta(seconds=4),
        timedelta(seconds=4),
        timedelta(seconds=4),
    ]


def test_the_delay_is_capped_and_never_exceeds_the_maximum() -> None:
    assert timedelta(seconds=4) == RETRY_MAX_DELAY
    for ordinal in range(1, 40):
        assert retry_delay(ordinal) <= RETRY_MAX_DELAY


def test_the_policy_is_deterministic_and_carries_no_jitter() -> None:
    """Two evaluations of the same ordinal must be identical, or a retry cannot be reconciled."""
    first = [retry_delay(ordinal) for ordinal in range(1, 6)]
    second = [retry_delay(ordinal) for ordinal in range(1, 6)]
    assert first == second
    assert PRODUCTION_RETRY_POLICY.delay(3) == retry_delay(3)


def test_the_ordinal_domain_is_bounded_by_the_schema_attempt_budget() -> None:
    """`max_attempts` is capped at 10, so the exponent can never grow without bound."""
    assert RETRY_MAX_ORDINAL == 10
    assert RETRY_BASE_DELAY * 2 ** (RETRY_MAX_ORDINAL - 1) > RETRY_MAX_DELAY


def test_an_invalid_ordinal_is_rejected_rather_than_silently_defaulted() -> None:
    with pytest.raises(ValueError):
        retry_delay(0)
    with pytest.raises(ValueError):
        retry_delay(-1)


def test_the_production_policy_satisfies_the_protocol() -> None:
    policy: RetryPolicy = CappedExponentialRetry()
    assert policy.delay(1) == timedelta(seconds=1)


def test_the_due_time_is_the_anchor_plus_the_policy_delay() -> None:
    assert retry_due_at(PRODUCTION_RETRY_POLICY, anchor_at=NOW, ordinal=1) == NOW + timedelta(
        seconds=1
    )
    assert retry_due_at(PRODUCTION_RETRY_POLICY, anchor_at=NOW, ordinal=3) == NOW + timedelta(
        seconds=4
    )


def test_the_same_anchor_and_ordinal_always_produce_the_same_due_time() -> None:
    """This is what makes an uncertain COMMIT reconcilable to exactly one expected value."""
    anchor = NOW + timedelta(seconds=7, microseconds=123)
    first = retry_due_at(PRODUCTION_RETRY_POLICY, anchor_at=anchor, ordinal=2)
    second = retry_due_at(PRODUCTION_RETRY_POLICY, anchor_at=anchor, ordinal=2)
    assert first == second
    # A later anchor would move the deadline, which is exactly why the anchor is captured once.
    later = retry_due_at(
        PRODUCTION_RETRY_POLICY, anchor_at=anchor + timedelta(seconds=1), ordinal=2
    )
    assert later != first


def test_the_due_time_preserves_the_anchor_instant_not_the_current_clock() -> None:
    anchor = NOW - timedelta(seconds=30)
    assert retry_due_at(PRODUCTION_RETRY_POLICY, anchor_at=anchor, ordinal=1) == anchor + timedelta(
        seconds=1
    )


def test_a_naive_anchor_is_rejected() -> None:
    with pytest.raises(ValueError):
        retry_due_at(PRODUCTION_RETRY_POLICY, anchor_at=datetime(2026, 9, 14), ordinal=1)
