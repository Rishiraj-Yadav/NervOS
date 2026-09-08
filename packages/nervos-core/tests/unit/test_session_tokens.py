"""Tests for opaque session-token primitives."""

import hashlib

import pytest
from nervos_core.infrastructure.security.session_tokens import SecureSessionTokens


def test_tokens_are_unique_cookie_safe_and_digest_to_binary_sha256() -> None:
    adapter = SecureSessionTokens()
    first = adapter.generate()
    second = adapter.generate()

    assert len(first) == 43
    assert first != second
    assert adapter.digest(first) == hashlib.sha256(first.encode("ascii")).digest()
    assert len(adapter.digest(first)) == 32


@pytest.mark.parametrize("token", ["", "short", "x" * 44, "!" * 43])
def test_malformed_token_is_rejected_before_lookup(token: str) -> None:
    with pytest.raises(ValueError, match="invalid session token"):
        SecureSessionTokens().digest(token)
