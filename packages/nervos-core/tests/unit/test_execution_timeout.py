"""C5 execution-deadline policy: what the deadline produces, and what may follow it.

The deadline itself is the accepted C2/C4 boundary — `provider_timeout_ms` from the Run's
immutable limits, applied around the single trusted provider call inside `RunExecutor`. C5 does
not re-implement it; it pins the *policy* around it, because the whole point of the C5 timeout is
that a locally-abandoned call is not evidence the remote provider did nothing.
"""

from __future__ import annotations

import pytest
from nervos_core.application.job_execution import RetryDisposition, disposition_for
from nervos_core.application.model_completion import (
    EXECUTION_CANCELLED,
    MODEL_AUTHENTICATION_FAILED,
    MODEL_RATE_LIMITED,
    MODEL_TIMED_OUT,
    MODEL_UNAVAILABLE,
    safe_error_message,
)


def test_a_timeout_is_never_treated_as_safe_to_replay() -> None:
    """A local deadline proves the wait ended, not that the provider never executed."""
    assert disposition_for(MODEL_TIMED_OUT) is RetryDisposition.AMBIGUOUS


def test_cancellation_is_never_treated_as_safe_to_replay() -> None:
    """Stopping the local wait says nothing about a request the provider already received."""
    assert disposition_for(EXECUTION_CANCELLED) is RetryDisposition.AMBIGUOUS


def test_transport_and_authentication_failures_keep_their_frozen_dispositions() -> None:
    assert disposition_for(MODEL_UNAVAILABLE) is RetryDisposition.AMBIGUOUS
    assert disposition_for(MODEL_AUTHENTICATION_FAILED) is RetryDisposition.DO_NOT_RETRY
    # The one positively safe code is unchanged by C5.
    assert disposition_for(MODEL_RATE_LIMITED) is RetryDisposition.SAFE_TO_RETRY


def test_an_unknown_code_still_fails_closed() -> None:
    assert disposition_for("some_future_code") is RetryDisposition.AMBIGUOUS


def test_every_c5_code_has_a_static_safe_message() -> None:
    """No code may be persisted without a NervOS-owned message that leaks nothing."""
    for code in (MODEL_TIMED_OUT, EXECUTION_CANCELLED):
        message = safe_error_message(code)
        assert message.strip()
        assert code not in message
        assert "http" not in message.lower()


def test_the_cancellation_message_describes_an_owner_decision_not_a_provider_fault() -> None:
    message = safe_error_message(EXECUTION_CANCELLED).lower()
    assert "cancel" in message
    assert "provider" not in message
    assert "model" not in message


def test_only_the_reviewed_codes_are_safe_to_retry() -> None:
    """C4's retry engine admits exactly one code; C5 must not have widened that set."""
    from nervos_core.application.model_completion import SAFE_ERROR_MESSAGES

    safe = [
        code
        for code in SAFE_ERROR_MESSAGES
        if disposition_for(code) is RetryDisposition.SAFE_TO_RETRY
    ]
    assert safe == [MODEL_RATE_LIMITED]


def test_a_timeout_outcome_cannot_be_constructed_without_a_safe_error() -> None:
    """The frozen outcome shape is what stops a deadline breach becoming a silent success."""
    from nervos_core.application.run_execution import ExecutionOutcome
    from nervos_core.domain.runs import ModelUsage

    with pytest.raises(ValueError):
        ExecutionOutcome("failed", None, None, ModelUsage(), 5, None, None)
