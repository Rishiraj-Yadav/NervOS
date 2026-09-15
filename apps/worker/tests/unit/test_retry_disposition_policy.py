"""C2 retry-disposition policy: evidence only, never acted on."""

from __future__ import annotations

import pytest
from nervos_core.application.job_execution import (
    DISPOSITION_BY_CODE,
    HEARTBEAT_INTERVAL,
    LEASE_DURATION,
    disposition_for,
)
from nervos_core.application.model_completion import (
    INTERNAL_EXECUTION_ERROR,
    MODEL_ACCOUNT_UNAVAILABLE,
    MODEL_AUTHENTICATION_FAILED,
    MODEL_OUTPUT_INCOMPLETE,
    MODEL_OUTPUT_TOO_LARGE,
    MODEL_PERMISSION_DENIED,
    MODEL_RATE_LIMITED,
    MODEL_REFUSED,
    MODEL_REQUEST_REJECTED,
    MODEL_RESPONSE_INVALID,
    MODEL_TIMED_OUT,
    MODEL_UNAVAILABLE,
    SAFE_ERROR_MESSAGES,
)
from nervos_core.domain.jobs import RetryDisposition


def test_the_lease_allows_at_least_three_heartbeats() -> None:
    assert LEASE_DURATION >= 3 * HEARTBEAT_INTERVAL


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (MODEL_RATE_LIMITED, RetryDisposition.SAFE_TO_RETRY),
        (MODEL_TIMED_OUT, RetryDisposition.AMBIGUOUS),
        (MODEL_UNAVAILABLE, RetryDisposition.AMBIGUOUS),
        (INTERNAL_EXECUTION_ERROR, RetryDisposition.AMBIGUOUS),
        (MODEL_AUTHENTICATION_FAILED, RetryDisposition.DO_NOT_RETRY),
        (MODEL_PERMISSION_DENIED, RetryDisposition.DO_NOT_RETRY),
        (MODEL_ACCOUNT_UNAVAILABLE, RetryDisposition.DO_NOT_RETRY),
        (MODEL_REQUEST_REJECTED, RetryDisposition.DO_NOT_RETRY),
        (MODEL_REFUSED, RetryDisposition.DO_NOT_RETRY),
        (MODEL_RESPONSE_INVALID, RetryDisposition.DO_NOT_RETRY),
        (MODEL_OUTPUT_INCOMPLETE, RetryDisposition.DO_NOT_RETRY),
        (MODEL_OUTPUT_TOO_LARGE, RetryDisposition.DO_NOT_RETRY),
    ],
)
def test_every_allowlisted_code_records_its_approved_disposition(
    code: str, expected: RetryDisposition
) -> None:
    assert DISPOSITION_BY_CODE[code] is expected
    assert disposition_for(code) is expected


def test_the_policy_covers_every_persistable_provider_code() -> None:
    """A newly allowlisted code must never silently default to a disposition."""
    provider_codes = {code for code in SAFE_ERROR_MESSAGES if code.startswith("model_")}
    assert provider_codes <= set(DISPOSITION_BY_CODE)


def test_an_unrecognized_code_fails_closed() -> None:
    assert disposition_for("something_unrecognized") is RetryDisposition.AMBIGUOUS


def test_no_missing_credential_code_was_introduced() -> None:
    """Capability absence is a queue state, so it has no execution outcome to record."""
    assert "model_provider_unavailable" not in DISPOSITION_BY_CODE
    assert "model_provider_unavailable" not in SAFE_ERROR_MESSAGES
