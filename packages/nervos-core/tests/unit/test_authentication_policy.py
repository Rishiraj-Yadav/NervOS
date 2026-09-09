"""Tests for authentication input policies."""

import pytest
from nervos_core.application.authentication import (
    InvalidPassword,
    InvalidUsername,
    canonicalize_username,
    validate_password,
)


def test_username_is_nfkc_trimmed_and_casefolded() -> None:
    assert canonicalize_username("  Ａdmin.User  ") == "admin.user"  # noqa: RUF001
    assert canonicalize_username("Straße") == "strasse"


@pytest.mark.parametrize(
    "username",
    ["abc", "a" * 32, "a_b.c-d", "A-B"],
)
def test_valid_username_boundaries_and_characters_are_accepted(username: str) -> None:
    assert canonicalize_username(username) == username.casefold()


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


def test_password_whitespace_is_preserved_exactly() -> None:
    password = "  exact password  "
    assert validate_password(password) == password
    whitespace_only = " " * 12
    assert validate_password(whitespace_only) == whitespace_only


def test_password_utf8_byte_boundary_and_invalid_unicode() -> None:
    assert validate_password("😀" * 128) == "😀" * 128
    with pytest.raises(InvalidPassword):
        validate_password("x" * 11 + "\ud800")
