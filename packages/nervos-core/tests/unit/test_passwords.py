"""Tests for the Argon2id password adapter."""

from argon2 import PasswordHasher
from argon2.low_level import Type
from nervos_core.infrastructure.security.passwords import Argon2PasswordHasher


def fast_hasher() -> Argon2PasswordHasher:
    return Argon2PasswordHasher(
        PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1, hash_len=32, type=Type.ID)
    )


def test_correct_password_verifies_and_wrong_password_fails() -> None:
    adapter = fast_hasher()
    encoded = adapter.hash("correct horse battery staple")

    assert encoded.startswith("$argon2id$")
    assert adapter.verify(encoded, "correct horse battery staple") is True
    assert adapter.verify(encoded, "wrong password") is False


def test_malformed_hash_fails_without_exception() -> None:
    assert fast_hasher().verify("not-a-password-hash", "some password") is False


def test_rehash_detection_uses_centralized_parameters() -> None:
    adapter = fast_hasher()
    encoded = adapter.hash("correct horse battery staple")

    assert adapter.needs_rehash(encoded) is False
    assert Argon2PasswordHasher().needs_rehash(encoded) is True


def test_dummy_verification_accepts_arbitrary_bounded_input() -> None:
    Argon2PasswordHasher().verify_dummy("unknown account password")
