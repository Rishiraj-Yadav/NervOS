"""Selected compat32 headers from bounded archive chunks, without retaining the body."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from email.policy import compat32
from typing import cast

_NEWLINE = re.compile(rb"\r\n|\r|\n")
_NAME = re.compile(rb"[\041-\071\073-\176]*\Z")
_FOLDED = re.compile(rb"(?:[ \t][^\r\n]*(?:\r\n|\n|\r(?!\n)))+\Z")
_CONTINUATION_END = re.compile(rb"\n[^ \t]")


class MetadataValueLimitExceeded(ValueError):
    """An approved consumed-field budget was exceeded before parsing its value."""


def selected_headers(
    chunks: Iterable[bytes],
    selected: frozenset[str],
    *,
    value_limits: Mapping[str, int] | None = None,
) -> Iterator[tuple[str, str]]:
    """Preserve header_source_parse/header_fetch_parse semantics for selected fields.

    Ignored physical lines use bounded fragments, even without a newline. The fast
    path discards runs of complete irrelevant headers in C. It never skips a
    selected header, continuation, malformed line, envelope line or separator.
    Exhaust the archive stream after the body starts: CRC/size checks still run.
    """
    names = b"|".join(re.escape(name.encode("ascii")) for name in sorted(selected))
    ignored_boundary = re.compile(
        rb"(?m)^(?=[\s\S])(?!(?!(?:" + names + rb"):)"
        rb"[\041-\071\073-\176]+:)",
        re.IGNORECASE,
    )
    key: str | None = None
    value = bytearray()
    line_start = True
    before_colon = False
    body = False
    pending = b""
    shared_values: dict[tuple[str, str], str] = {}

    def append(fragment: bytes) -> None:
        if key is not None and value_limits and key in value_limits and not value:
            fragment = fragment.lstrip(b" \t")
        value.extend(fragment)
        if (
            key is not None
            and value_limits
            and key in value_limits
            and len(value) > value_limits[key] + 2
        ):
            raise MetadataValueLimitExceeded("Requires-Dist logical value budget exceeded")

    def finish() -> tuple[str, str] | None:
        if key is None:
            return None
        # This is compat32.header_source_parse's exact value construction; unlike
        # a full Message, it needs neither a list of all lines nor ignored fields.
        text = value.decode("ascii", "surrogateescape").lstrip(" \t").rstrip("\r\n")
        if (
            value_limits
            and key in value_limits
            and len(text.encode("ascii", "surrogateescape")) > value_limits[key]
        ):
            raise MetadataValueLimitExceeded("Requires-Dist logical value budget exceeded")
        # compat32 returns an email.header.Header for surrogate bytes despite its
        # typeshed str annotation. Preserve that old runtime behavior as well.
        parsed = cast(object, compat32.header_fetch_parse(key, text))
        # Share equal short strings without removing any header occurrence. This
        # avoids millions of copies of identical requirement values. The cache
        # bounds only an optimization; larger/unique values still pass unchanged.
        if isinstance(parsed, str) and len(parsed) <= 1024:
            token = key, parsed
            if token in shared_values:
                parsed = shared_values[token]
            elif len(shared_values) < 128:
                shared_values[token] = parsed
        return key, cast(str, parsed)

    def with_end() -> Iterator[tuple[bytes, bool]]:
        for chunk in chunks:
            yield chunk, False
        yield b"", True

    for incoming, final in with_end():
        if body:
            continue
        chunk = pending + incoming
        pending = b""
        if not final:
            # Hold only a short unfinished prefix (or a CR split from LF).
            # This permits arbitrary source chunk boundaries without storing a
            # whole ignored physical line. Selected names are shorter than 32.
            tail = max(chunk.rfind(b"\n"), chunk.rfind(b"\r")) + 1
            if chunk.endswith(b"\r"):
                tail -= 1
            if 0 < len(chunk) - tail < 32:
                pending, chunk = chunk[tail:], chunk[:tail]
        lf_lines = b"\r" not in chunk.replace(b"\r\n", b"")
        if line_start and chunk.startswith((b" ", b"\t")) and b"\r" not in chunk:
            # Find the next field/body boundary in C, without a regex stack frame
            # per folded line. Every intervening LF line starts with SP/HT and
            # is an exact compat32 continuation (or an ignored leading defect).
            boundary = _CONTINUATION_END.search(chunk)
            end = boundary.start() + 1 if boundary else len(chunk)
            prefix = chunk[:end]
            if key is not None:
                append(prefix)
            line_start = prefix.endswith(b"\n")
            if end == len(chunk):
                continue
            chunk = chunk[end:]
        if line_start and not before_colon and chunk and _FOLDED.fullmatch(chunk):
            if key is not None:
                append(chunk)
            continue
        offset = 0
        while offset < len(chunk):
            if line_start and lf_lines:
                next_field = ignored_boundary.search(chunk, offset)
                ignored_end = next_field.start() if next_field else len(chunk)
                if ignored_end > offset:
                    if result := finish():
                        yield result
                    key, value = None, bytearray()
                    before_colon = False
                    line_start = chunk[ignored_end - 1] == 10
                    offset = ignored_end
                    continue
            newline = _NEWLINE.search(chunk, offset)
            end = newline.end() if newline else len(chunk)
            fragment = chunk[offset:end]
            if line_start:
                if fragment.startswith((b" ", b"\t")):
                    # Leading continuations are defects ignored by compat32.
                    before_colon = False
                else:
                    if result := finish():
                        yield result
                    key, value = None, bytearray()
                    if fragment.startswith(b"From "):
                        before_colon = False
                    elif fragment.startswith((b"\r", b"\n")):
                        body = True
                        break
                    else:
                        colon = fragment.find(b":")
                        prefix = fragment[:colon] if colon >= 0 else fragment
                        if not _NAME.fullmatch(prefix):
                            body = True
                            break
                        before_colon = colon < 0
                        if colon > 0:
                            name = prefix.decode("ascii").lower()
                            if name in selected:
                                key = name
                                fragment = fragment[colon + 1 :]
                        elif colon == 0:
                            before_colon = False
            elif before_colon:
                colon = fragment.find(b":")
                prefix = fragment[:colon] if colon >= 0 else fragment
                if not _NAME.fullmatch(prefix):
                    body = True
                    break
                before_colon = colon < 0
            if key is not None:
                append(fragment)
            line_start = newline is not None
            offset = end
    if not body and (result := finish()):
        yield result
