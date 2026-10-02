"""Compare streamed normalized text against the authoritative packaging parser."""

from __future__ import annotations

import itertools
import random
from collections.abc import Callable

import pytest
from nervos_core.application.package_wheel_version import normalized_metadata_version
from packaging.version import Version


def outcome(operation: Callable[[], str]) -> object:
    try:
        return operation()
    except ValueError as error:
        return type(error), str(error)


def equivalent(value: str) -> None:
    assert outcome(lambda: normalized_metadata_version(value)) == outcome(
        lambda: str(Version(value))
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "1",
        "0.01.000",
        "v1",
        "V000!01.002",
        "001!1.2",
        "1.0-01",
        "1.0alpha",
        "1.0.preview-002",
        "1.0REV",
        "1.0r_01",
        "1.0-dev",
        "1.0a1.post2.dev3+A.000_B-02",
        "1+000",
        "1+AB_c-d",
        "\t1.0\r\n",
        "\u20031.0\u2003",
        "\u0661.\u0662",
        "1+\u212a",
        "1+İ",
        "1+\u017f",
        "1+\u0131",
        "1..0",
        "1.0+",
        "1.0+a..b",
        "1.0+a/",
        "1.0post1a1",
        "1.0\n+abc",
        "1.0dev2.post1",
        "1.0--1",
        ".1",
        "1.",
        "01!000.000rc000-000dev000",
        "1" * 5000,
        "1" * 5000 + "..1",
        "1+" + "1" * 5000,
        "1a" + "1" * 5000,
        "1" * 5000 + "!1",
        "1+" + "0" * 5000,
    ],
)
def test_normalization_and_errors_match_packaging(value: str) -> None:
    equivalent(value)


def test_generated_aliases_separators_and_segments_match_packaging() -> None:
    for epoch, release, pre, post, dev, local in itertools.product(
        ("", "0!", "01!"),
        ("1", "01.002.000"),
        ("", "alpha", "-B_02", ".preview-3", "rc0"),
        ("", "-01", "_REV_2", ".post"),
        ("", "dev", "-dev_02"),
        ("", "+A_000-b.02", "+001"),
    ):
        equivalent(epoch + release + pre + post + dev + local)


def test_deterministic_malformed_corpus_matches_packaging() -> None:
    generator = random.Random(440)
    alphabet = "0123abcVvr._-+! \t\r\n/α١Kİ"
    for _ in range(4000):
        equivalent("".join(generator.choices(alphabet, k=generator.randrange(0, 35))))


@pytest.mark.parametrize("tail", ["", "garbage", "+", ".", "\n", "\x00"])
def test_large_local_and_release_segments_match_packaging(tail: str) -> None:
    equivalent("000!001.002rc03.post04.dev05+" + "a.0002_B-03." * 10_000 + "z" + tail)
    equivalent("000!" + "001.002." * 10_000 + "003rc04.post05.dev06+a" + tail)


def test_more_than_cache_capacity_and_large_single_local_segment() -> None:
    equivalent("1+" + ".".join("a" + str(index) for index in range(5000)))
    equivalent("1+" + "ABC" * 30_000)
