"""C2 allowlisted safe-error vocabulary tests."""

from __future__ import annotations

import re

import pytest
from nervos_core.application.model_completion import (
    EXECUTION_OUTCOME_AMBIGUOUS,
    INTERNAL_EXECUTION_ERROR,
    MODEL_AUTHENTICATION_FAILED,
    MODEL_PERMISSION_DENIED,
    MODEL_RATE_LIMITED,
    MODEL_TIMED_OUT,
    MODEL_UNAVAILABLE,
    SAFE_ERROR_MESSAGES,
    provider_error_message,
    safe_error_message,
)

SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def test_every_allowlisted_code_fits_the_persisted_column_shape() -> None:
    assert SAFE_ERROR_MESSAGES
    for code in SAFE_ERROR_MESSAGES:
        assert SAFE_CODE.fullmatch(code), code


def test_every_allowlisted_message_is_static_bounded_and_non_blank() -> None:
    for code, message in SAFE_ERROR_MESSAGES.items():
        assert message.strip(), code
        assert "\x00" not in message, code
        assert len(message) <= 512, code
        assert len(message.encode("utf-8")) <= 2048, code


def test_the_infrastructural_closeout_code_lives_in_the_same_allowlist() -> None:
    """There is exactly one place that decides which codes may be persisted."""
    assert EXECUTION_OUTCOME_AMBIGUOUS in SAFE_ERROR_MESSAGES
    assert provider_error_message(EXECUTION_OUTCOME_AMBIGUOUS) == safe_error_message(
        EXECUTION_OUTCOME_AMBIGUOUS
    )
    assert "worker" not in safe_error_message(EXECUTION_OUTCOME_AMBIGUOUS).lower()


def test_an_unknown_code_is_refused_rather_than_persisted_as_itself() -> None:
    assert (
        safe_error_message("SYNTHETIC-LEAK-DO-NOT-PERSIST")
        == SAFE_ERROR_MESSAGES[INTERNAL_EXECUTION_ERROR]
    )
    with pytest.raises(KeyError):
        provider_error_message("not_a_real_code")


def test_no_new_missing_credential_execution_code_was_introduced() -> None:
    """Capability absence is a queue state, never an execution outcome."""
    assert "model_provider_unavailable" not in SAFE_ERROR_MESSAGES


@pytest.mark.parametrize(
    "code",
    [
        MODEL_AUTHENTICATION_FAILED,
        MODEL_PERMISSION_DENIED,
        MODEL_RATE_LIMITED,
        MODEL_TIMED_OUT,
        MODEL_UNAVAILABLE,
    ],
)
def test_known_provider_codes_resolve_to_their_static_message(code: str) -> None:
    assert provider_error_message(code) == SAFE_ERROR_MESSAGES[code]
