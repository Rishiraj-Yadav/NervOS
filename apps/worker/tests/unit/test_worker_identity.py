"""Worker process identity: bounds, uniqueness, and non-secrecy."""

from __future__ import annotations

import re

from nervos_worker.identity import MAX_WORKER_ID_LENGTH, generate_worker_id

ALLOWED = re.compile(r"[A-Za-z0-9._-]+\Z")


def test_identifier_is_bounded_and_repeatable_in_shape() -> None:
    identifier = generate_worker_id()

    assert 1 <= len(identifier) <= MAX_WORKER_ID_LENGTH
    assert len(identifier) <= 128  # the schema's claimed_by / worker_id bound
    assert ALLOWED.fullmatch(identifier), identifier


def test_identifiers_are_unique_across_calls() -> None:
    identifiers = {generate_worker_id() for _ in range(200)}

    assert len(identifiers) == 200


def test_identifier_carries_no_secret_content() -> None:
    identifier = generate_worker_id()

    for forbidden in ("sk-", "api_key", "ANTHROPIC", "OPENAI", "Bearer", "token"):
        assert forbidden not in identifier
