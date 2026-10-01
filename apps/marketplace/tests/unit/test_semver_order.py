import random

import pytest
from nervos_core.domain.packages import PackageVersion
from nervos_marketplace_service.domain.semver_order import precedence_key

EDGE = [
    "1.9.0",
    "1.10.0",
    "1.0.0-alpha",
    "1.0.0-alpha.1",
    "1.0.0-alpha.beta",
    "1.0.0-beta",
    "1.0.0-beta.2",
    "1.0.0-beta.11",
    "1.0.0-rc.1",
    "1.0.0",
    "1.0.0+a",
    "1.0.0+b",
    "0.0.0-0",
    "0.0.0-1",
    "0.0.0-1a",
    "0.0.0-a-b",
    "0.0.0-a.b",
    "0.0.0-a",
    "0.0.0-a.a",
    "9" * 55 + ".0.0",
    "0.0.0-" + "9" * 55,
]


def versions() -> list[PackageVersion]:
    rng = random.Random(7201)
    result = [PackageVersion(value) for value in EDGE]
    for _ in range(400):
        value = ".".join(str(rng.randrange(10 ** rng.randint(1, 12))) for _ in range(3))
        parts = [
            rng.choice(["alpha", "a", "a-b", "b0", str(rng.randrange(10**12))])
            for _ in range(rng.randint(0, 3))
        ]
        if parts:
            value += "-" + ".".join(parts)
        if rng.randrange(2):
            value += "+build." + str(rng.randrange(100))
        if len(value) <= 64:
            result.append(PackageVersion(value))
    return result


@pytest.mark.parametrize("left", EDGE)
def test_differential(left: str) -> None:
    a = PackageVersion(left)
    for b in versions():
        assert (a < b) == (precedence_key(a) < precedence_key(b))
        assert (b < a) == (precedence_key(b) < precedence_key(a))


def test_all_generated_pairs() -> None:
    generated = versions()
    keys = [precedence_key(value) for value in generated]
    for i, left in enumerate(generated):
        for j, right in enumerate(generated):
            assert (left < right) == (keys[i] < keys[j])
    assert precedence_key(PackageVersion("1.0.0+a")) == precedence_key(PackageVersion("1.0.0+b"))
