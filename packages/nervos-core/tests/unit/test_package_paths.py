"""G2 canonical path validation and cross-platform collision rules.

These are the rules that decide whether a package written on one operating system can be safely
read on another, so the cases below are adversarial rather than illustrative: Windows device names,
trailing dots and spaces, case collisions, and normalization collisions are all things that are
harmless on Linux and dangerous on Windows.
"""

from __future__ import annotations

import pytest
from nervos_core.application.package_paths import (
    MAX_PATH_BYTES,
    MAX_PATH_COMPONENT_BYTES,
    DuplicatePackageEntry,
    PackageSizeLimitExceeded,
    UnsafePackagePath,
    canonical_order,
    canonical_package_path,
    collision_key,
    validate_unique_package_paths,
)


@pytest.mark.parametrize(
    "path",
    [
        "manifest.yaml",
        "agent.whl",
        "assets/logo.png",
        "dependencies/wheels/helper_lib-2.0.0-py3-none-any.whl",
        "a/b/c/d.txt",
        "assets/Ünicode-ñame.txt",
    ],
)
def test_canonical_paths_are_accepted(path: str) -> None:
    assert canonical_package_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "/absolute/path.txt",
        "C:/drive/path.txt",
        "C:\\drive\\path.txt",
        "\\\\server\\share\\file.txt",
        "//server/share/file.txt",
        "trailing/",
        "double//slash.txt",
        "./relative.txt",
        "../escape.txt",
        "a/../../escape.txt",
        "a/./b.txt",
        "back\\slash.txt",
        "",
        "nul\x00byte.txt",
        "control\x01char.txt",
        "assets/..",
        "..",
        ".",
    ],
)
def test_unsafe_paths_are_rejected(path: str) -> None:
    with pytest.raises((UnsafePackagePath, PackageSizeLimitExceeded)):
        canonical_package_path(path)


@pytest.mark.parametrize(
    "component",
    ["CON", "con", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9", "CON.txt", "nul.tar.gz"],
)
def test_windows_reserved_device_names_are_rejected(component: str) -> None:
    """Windows reserves these even with an extension, so `CON.txt` is not a safe filename."""
    with pytest.raises(UnsafePackagePath):
        canonical_package_path(f"assets/{component}")


@pytest.mark.parametrize("component", ["COM0", "COM10", "LPT0", "CONSOLE", "NULL", "AUXILIARY"])
def test_similar_but_unreserved_names_are_accepted(component: str) -> None:
    """The rule is a fixed enumeration, not a prefix match, so near-misses stay usable."""
    assert canonical_package_path(f"assets/{component}.txt") == f"assets/{component}.txt"


@pytest.mark.parametrize("component", ["name.", "name ", "name. ", "report.py."])
def test_trailing_dots_and_spaces_are_rejected(component: str) -> None:
    """Windows silently strips these, which would make two distinct names one file."""
    with pytest.raises(UnsafePackagePath):
        canonical_package_path(f"assets/{component}")


def test_component_length_bound_is_enforced() -> None:
    assert canonical_package_path("a" * MAX_PATH_COMPONENT_BYTES) == "a" * MAX_PATH_COMPONENT_BYTES
    with pytest.raises(PackageSizeLimitExceeded):
        canonical_package_path("a" * (MAX_PATH_COMPONENT_BYTES + 1))


def test_total_path_length_bound_is_enforced() -> None:
    """Many components each within their own bound can still exceed the total path bound."""
    component = "a" * 200
    too_long = "/".join([component] * 6)
    assert len(too_long.encode("utf-8")) > MAX_PATH_BYTES
    with pytest.raises(PackageSizeLimitExceeded):
        canonical_package_path(too_long)


def test_byte_length_not_character_length_is_measured() -> None:
    """A multibyte name must be measured in UTF-8 bytes, not code points."""
    name = "é" * (MAX_PATH_COMPONENT_BYTES // 2 + 1)
    assert len(name) < MAX_PATH_COMPONENT_BYTES
    with pytest.raises(PackageSizeLimitExceeded):
        canonical_package_path(name)


class TestCollisionKey:
    def test_case_collision_is_detected(self) -> None:
        assert collision_key("assets/Foo.txt") == collision_key("assets/foo.txt")

    def test_normalization_collision_is_detected(self) -> None:
        """`e` + combining acute composes to `é` under NFC, so these are one name."""
        composed = "assets/caf\u00e9.txt"
        decomposed = "assets/cafe\u0301.txt"
        assert composed != decomposed
        assert collision_key(composed) == collision_key(decomposed)

    def test_distinct_paths_do_not_collide(self) -> None:
        assert collision_key("assets/a.txt") != collision_key("assets/b.txt")

    def test_separator_is_preserved(self) -> None:
        assert collision_key("a/b") != collision_key("ab")


def test_duplicate_paths_are_rejected() -> None:
    with pytest.raises(DuplicatePackageEntry):
        validate_unique_package_paths(["assets/a.txt", "assets/a.txt"])


def test_case_colliding_paths_are_rejected() -> None:
    with pytest.raises(DuplicatePackageEntry):
        validate_unique_package_paths(["assets/Foo.txt", "assets/foo.txt"])


def test_normalization_colliding_paths_are_rejected() -> None:
    with pytest.raises(DuplicatePackageEntry):
        validate_unique_package_paths(["assets/caf\u00e9.txt", "assets/cafe\u0301.txt"])


def test_unique_paths_pass_through_in_order() -> None:
    paths = ["assets/b.txt", "assets/a.txt"]
    assert validate_unique_package_paths(paths) == ("assets/b.txt", "assets/a.txt")


def test_canonical_order_is_bytewise_and_deterministic() -> None:
    paths = ["b.txt", "a.txt", "B.txt", "assets/z.txt"]
    assert canonical_order(paths) == tuple(sorted(paths, key=lambda item: item.encode("utf-8")))
    assert canonical_order(paths) == canonical_order(reversed(paths))


def test_canonical_order_is_independent_of_input_order() -> None:
    """Ordering comes from the paths themselves, never from enumeration order."""
    import random

    paths = [f"assets/file-{index}.txt" for index in range(30)]
    shuffled = list(paths)
    random.Random(1234).shuffle(shuffled)
    assert canonical_order(shuffled) == canonical_order(paths)
