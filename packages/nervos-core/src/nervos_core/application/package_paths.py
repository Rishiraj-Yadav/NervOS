"""Canonical `.nervos` archive path validation and cross-platform collision detection.

This module is the **single** path authority for Stage G2: the builder and the verifier both call
these functions, so a package that one accepts can never be one the other rejects. There is
deliberately no second path implementation anywhere in the package system.

The rules exist because a `.nervos` archive is written on one operating system and inspected on
another. A path that is harmless on Linux (`CON`, `aux.txt`, `a` plus a trailing space, or two names
that differ only by case) is dangerous or ambiguous on Windows, so the format rejects it everywhere
rather than accepting it on one platform and failing on another.

Validation is **validation-only**: a path is never silently rewritten into a safe one. That mirrors
the frozen identifier rule in ADR 0024 ("no silent normalization or Unicode rewriting") -- a package
either declares an acceptable path or is rejected.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable

# Frozen cross-platform path bounds. These are code-level maxima: no setting may raise them.
MAX_PATH_COMPONENT_BYTES = 255
MAX_PATH_BYTES = 1024

# Windows reserved device names. Per Windows semantics these are reserved even when they carry an
# extension (`CON.txt` is still the console device), so only the portion before the first dot is
# compared.
_RESERVED_DEVICE_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)

_SEPARATOR = "/"


class UnsafePackagePath(ValueError):
    """Raised when an archive member path is not a safe canonical package path."""


class DuplicatePackageEntry(ValueError):
    """Raised when two archive members collide under the canonical collision key."""


class PackageSizeLimitExceeded(ValueError):
    """Raised when a path, member, or archive exceeds a frozen size bound."""


def _describe(component: str) -> str:
    """A diagnostic-safe rendering: lengths and shape, never the raw untrusted bytes."""
    return f"{len(component)}-character component"


def _validate_component(component: str) -> None:
    """Validate one already-split path component."""
    if not component:
        raise UnsafePackagePath("path components must not be empty")
    if component in {".", ".."}:
        raise UnsafePackagePath("path components must not be '.' or '..'")
    if any(ord(character) < 32 or ord(character) == 127 for character in component):
        raise UnsafePackagePath(f"{_describe(component)} contains a control character")

    encoded = component.encode("utf-8")
    if len(encoded) > MAX_PATH_COMPONENT_BYTES:
        raise PackageSizeLimitExceeded(f"path component exceeds {MAX_PATH_COMPONENT_BYTES} bytes")

    # Windows silently strips trailing dots and spaces, so `report.py ` and `report.py` would name
    # the same file on Windows and different files on Linux. Rather than pick one meaning, reject.
    if component != component.rstrip(". "):
        raise UnsafePackagePath("path components must not end with a space or a period")

    device = component.split(".", 1)[0].casefold()
    if device in _RESERVED_DEVICE_NAMES:
        raise UnsafePackagePath("path component is a reserved device name")


def canonical_package_path(value: object) -> str:
    """Validate and return one canonical relative archive path.

    Accepts exactly the form the archive format permits: a non-empty relative path using `/`
    separators, with no backslash, no drive or UNC prefix, no empty/`.`/`..` component, no control
    character, and every component inside the frozen byte bounds.
    """
    if not isinstance(value, str) or not value:
        raise UnsafePackagePath("path must be non-empty text")
    if "\\" in value:
        raise UnsafePackagePath("path must use '/' separators, not backslashes")
    if value.startswith(_SEPARATOR):
        raise UnsafePackagePath("path must be relative")
    if value.startswith("//"):
        raise UnsafePackagePath("path must not be a UNC path")
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        raise UnsafePackagePath("path must not be a drive path")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise UnsafePackagePath("path contains a control character")

    if len(value.encode("utf-8")) > MAX_PATH_BYTES:
        raise PackageSizeLimitExceeded(f"path exceeds {MAX_PATH_BYTES} bytes")

    for component in value.split(_SEPARATOR):
        _validate_component(component)
    return value


def collision_key(value: str) -> str:
    """The one normalized key used for every archive collision class.

    Unicode NFC composition plus `casefold` per component. NFC folds canonically-equivalent
    sequences (`e` + combining acute vs `é`) and `casefold` folds case, so this single key catches
    exact duplicates, case-insensitive collisions (Windows), and normalization collisions at once.
    NFKC is deliberately *not* used: compatibility folding would map distinct characters together
    for no archive-safety reason.
    """
    normalized = unicodedata.normalize("NFC", value)
    return _SEPARATOR.join(component.casefold() for component in normalized.split(_SEPARATOR))


def validate_unique_package_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """Validate every path and reject any collision, returning the canonical paths in order.

    Deterministic: collisions are reported against the first path in iteration order that claimed a
    key, so the error is stable regardless of how the caller enumerated its entries.
    """
    validated: list[str] = []
    claimed: dict[str, str] = {}
    for raw in paths:
        path = canonical_package_path(raw)
        key = collision_key(path)
        existing = claimed.get(key)
        if existing is not None:
            raise DuplicatePackageEntry(
                f"archive members collide under cross-platform comparison "
                f"({existing!r} and {path!r})"
            )
        claimed[key] = path
        validated.append(path)
    return tuple(validated)


def canonical_order(paths: Iterable[str]) -> tuple[str, ...]:
    """Return paths in the frozen canonical archive order.

    Ordering is byte-wise on the UTF-8 encoding, never filesystem enumeration order and never
    locale-dependent string collation, so two builds from different machines agree.
    """
    return tuple(sorted(paths, key=lambda path: path.encode("utf-8")))
