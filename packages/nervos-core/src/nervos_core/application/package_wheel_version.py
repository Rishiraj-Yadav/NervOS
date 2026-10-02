"""PEP 440 normalized metadata text without a tuple per release/local segment."""

from __future__ import annotations

import io
import re

from packaging.version import VERSION_PATTERN, InvalidVersion, Version

# Use packaging's public grammar, including its flags and surrounding whitespace.
# There is no independently maintained version grammar or comparison operation.
_VERSION = re.compile(r"\s*" + VERSION_PATTERN + r"\s*", re.VERBOSE | re.IGNORECASE)
_RELEASE_PART = re.compile(r"[0-9]+")
_LOCAL_PART = re.compile(r"[a-z0-9]+", re.ASCII | re.IGNORECASE)


def normalized_metadata_version(value: str) -> str:
    """Match the same grammar, then normalize segments into bounded output chunks.

    Wheel inspection needs only str(Version(value)), not a sortable Version or
    millions of retained local/release objects. Numeric conversion and suffix
    normalization still use Python int and packaging.Version, in their original
    order. No value, segment count or accepted format limit is introduced.
    """
    match = _VERSION.fullmatch(value)
    if match is None:
        raise InvalidVersion(f"Invalid version: {value!r}")
    epoch = int(match.group("epoch")) if match.group("epoch") else 0
    output = io.StringIO()
    if epoch:
        output.write(str(epoch) + "!")
    cached: dict[str, str] = {}

    def parts(name: str, pattern: re.Pattern[str], *, local: bool) -> None:
        start, end = match.span(name)
        pending: list[str] = []
        pending_bytes = 0
        first = True
        for part in pattern.finditer(value, start, end):
            raw = part.group()
            if raw in cached:
                normalized = cached[raw]
            else:
                normalized = str(int(raw)) if not local or raw.isdigit() else raw.lower()
                if len(raw) <= 1024 and len(cached) < 128:
                    cached[raw] = normalized
            token = normalized if first else "." + normalized
            first = False
            pending.append(token)
            pending_bytes += len(token)
            if pending_bytes >= 65536:
                output.write("".join(pending))
                pending.clear()
                pending_bytes = 0
        output.write("".join(pending))

    parts("release", _RELEASE_PART, local=False)
    suffix = "".join(match.group(name) or "" for name in ("pre", "post", "dev"))
    if suffix:
        # A compact artificial release delegates every suffix alias/default to
        # packaging. Its normalized leading '0' is then removed from the text.
        output.write(str(Version("0" + suffix))[1:])
    if match.start("local") != -1:
        output.write("+")
        parts("local", _LOCAL_PART, local=True)
    return output.getvalue()
