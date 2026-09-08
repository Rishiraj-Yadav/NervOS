"""Tests for authentication input policies."""

import pytest
from nervos_core.application.authentication import (
    InvalidPassword,
    InvalidUsername,
    canonicalize_username,
    validate_password,
)


def test_username_is_nfkc_trimmed_and_lowercase() -> None:
    assert canonicalize_username("  Ａdmin.User  ") == "admin.user"  # noqa: RUF001


@pytest.mark.parametrize("username", ["ab", "a" * 33, "bad user", "-admin", "admin/"])
def test_invalid_username_is_rejected(username: str) -> None:
    with pytest.raises(InvalidUsername):
        canonicalize_username(username)


@pytest.mark.parametrize("length", [12, 128])
def test_password_boundaries_are_accepted(length: int) -> None:
    password = "x" * length
    assert validate_password(password) == password


@pytest.mark.parametrize("length", [11, 129])
def test_password_outside_boundaries_is_rejected(length: int) -> None:
    with pytest.raises(InvalidPassword):
        validate_password("x" * length)
