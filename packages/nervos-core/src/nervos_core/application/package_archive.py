"""Bounded, deterministic ZIP primitives for the `.nervos` archive format.

Two jobs, one module, because the builder and verifier must not diverge:

* writing canonical ZIP bytes whose metadata is fixed rather than inherited from the host; and
* reading untrusted ZIP bytes through bounded streams that never extract to disk.

The reader is the security-sensitive half. A `.nervos` archive is attacker-controlled input, so
declared sizes in the central directory are treated as claims: they are checked cheaply first to
reject obvious abuse, and then re-checked while streaming, which is what makes the frozen limits
real rather than advisory. Nothing here decompresses more than it has already budgeted for.
"""

from __future__ import annotations

import io
import os
import stat
import zipfile
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from nervos_core.application.package_paths import (
    DuplicatePackageEntry,
    PackageSizeLimitExceeded,
    canonical_order,
    canonical_package_path,
    collision_key,
)

# Frozen Stage-G archive limits (Stage-G plan §27 / ADR 0026). Code-level maxima: operator settings
# may only tighten them, never raise them, so none of these is read from configuration.
ARCHIVE_MAX_BYTES = 256 * 1024 * 1024
EXTRACTED_MAX_BYTES = 1024 * 1024 * 1024
MAX_ENTRIES = 20_000
SINGLE_FILE_MAX_BYTES = 256 * 1024 * 1024

# Streamed reads never buffer more than this, so verifying a package cannot require memory
# proportional to its payload.
READ_CHUNK_BYTES = 64 * 1024

# Fixed ZIP metadata. Every one of these is written explicitly because `zipfile` otherwise derives
# values from the host: `create_system` comes from `sys.platform`, and per-file metadata comes from
# the filesystem when `ZipInfo.from_file` is used. None of that may leak into a portable artifact.
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
FIXED_EXTERNAL_ATTR = (stat.S_IFREG | 0o644) << 16
FIXED_CREATE_SYSTEM = 3
FIXED_INTERNAL_ATTR = 0
FIXED_EXTRA = b""
FIXED_COMMENT = b""


class PackageArchiveError(ValueError):
    """Raised when an archive is malformed, truncated, or not a ZIP at all."""


class ArchiveValidationProfile(StrEnum):
    """Explicitly selects outer `.nervos` or nested wheel ZIP policy."""

    NERVOS_V1 = "nervos-v1"
    WHEEL = "wheel"


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    """One archive entry's validated metadata, without its payload bytes."""

    path: str
    size: int
    compression: int


def canonical_zip_info(path: str, size: int) -> zipfile.ZipInfo:
    """Build a `ZipInfo` whose metadata is fully fixed, not host-derived.

    `size` is supplied so the writer can emit the member without a data descriptor; a member written
    with a known size produces different (and non-seekable) bytes than one streamed behind a
    descriptor, so this is a determinism requirement and not only a convenience.
    """
    info = zipfile.ZipInfo(filename=path, date_time=FIXED_DATE_TIME)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = FIXED_CREATE_SYSTEM
    info.external_attr = FIXED_EXTERNAL_ATTR
    info.internal_attr = FIXED_INTERNAL_ATTR
    info.file_size = size
    info.extra = FIXED_EXTRA
    info.comment = FIXED_COMMENT
    info.flag_bits |= 0x800  # the UTF-8 / language-encoding flag; names are always UTF-8
    return info


def reject_undeclared_nested_archives(
    payloads: Mapping[str, bytes],
    *,
    allowed_paths: frozenset[str],
) -> None:
    """Reject archive content outside explicitly declared wheel members.

    ZIP detection uses both magic and the bounded parser. TAR has no reliable leading magic, so its
    standard `ustar` marker at byte 257 is checked as well. Filename extensions are intentionally
    irrelevant: renaming an archive to `.bin` cannot bypass this rule.
    """
    for path, payload in payloads.items():
        if path in allowed_paths:
            continue
        is_zip = payload.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
        is_tar = len(payload) >= 262 and payload[257:262] == b"ustar"
        if is_tar:
            raise PackageArchiveError(f"undeclared nested archive at {path!r}")
        if not is_zip:
            continue
        try:
            BoundedArchiveReader(payload, profile=ArchiveValidationProfile.WHEEL)
        except ValueError as error:
            raise PackageArchiveError(
                f"undeclared nested archive at {path!r} is malformed"
            ) from error
        raise PackageArchiveError(f"undeclared nested archive at {path!r}")


def write_canonical_archive_file(entries: Mapping[str, Path], destination: Path) -> None:
    """Stream canonical entries from immutable snapshot files into one seekable ZIP file."""
    ordered = canonical_order(entries)
    with (
        destination.open("w+b") as output,
        zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_STORED) as archive,
    ):
        archive.comment = FIXED_COMMENT
        for path in ordered:
            source = entries[path]
            size = source.stat().st_size
            info = canonical_zip_info(path, size)
            info.create_version = 63  # Unix-style ZIP version for create_system=3
            written = 0
            with (
                source.open("rb") as reader,
                archive.open(info, mode="w", force_zip64=False) as writer,
            ):
                while chunk := reader.read(READ_CHUNK_BYTES):
                    written += len(chunk)
                    if written > size:
                        raise PackageArchiveError("snapshot changed while writing the archive")
                    writer.write(chunk)
            if written != size:
                raise PackageArchiveError("snapshot changed while writing the archive")
        output.flush()
        os.fsync(output.fileno())


def write_canonical_archive(entries: Mapping[str, bytes]) -> bytes:
    """Write canonical `.nervos` archive bytes from canonical path -> exact payload bytes.

    The outer container uses `ZIP_STORED`: the payloads are already-compressed wheels and
    NervOS-generated metadata, so compressing again buys nothing and would introduce a `zlib`
    version and level as determinism variables. Payload bytes are written exactly as supplied --
    never re-encoded, re-compressed, or line-ending-normalized (Stage-G §26: payload bytes are
    hashed and stored as exact bytes).

    No directory entries are written. A ZIP directory member carries no payload and would add an
    entry whose metadata has to be canonicalized for no benefit; readers derive directories from the
    file paths.
    """
    ordered = canonical_order(entries)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_STORED) as archive:
        for path in ordered:
            payload = entries[path]
            archive.writestr(canonical_zip_info(path, len(payload)), payload)
    return buffer.getvalue()


def _allowed_compression(profile: ArchiveValidationProfile) -> frozenset[int]:
    if profile is ArchiveValidationProfile.NERVOS_V1:
        return frozenset({zipfile.ZIP_STORED})
    return frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


def _reject_unsafe_wheel_type(info: zipfile.ZipInfo) -> None:
    """Reject Unix special files while allowing ordinary wheel metadata from either host style."""
    if info.create_system != 3:
        return
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if file_type not in {0, stat.S_IFREG}:
        raise PackageArchiveError("wheel must contain ordinary files only")


class BoundedArchiveReader:
    """Read a ZIP archive through bounded streams, never extracting to disk.

    Path and seekable-source inputs remain file-backed. The bytes constructor is a convenience for
    small callers and tests; it delegates to the same scan and member-read implementation.
    """

    def __init__(
        self,
        source: Path | BinaryIO | bytes,
        *,
        profile: ArchiveValidationProfile,
    ) -> None:
        self._profile = profile
        self._path: Path | None = source if isinstance(source, Path) else None
        self._stream: BinaryIO | None = None
        self._bytes: bytes | None = None
        if isinstance(source, bytes):
            if len(source) > ARCHIVE_MAX_BYTES:
                raise PackageSizeLimitExceeded(f"archive exceeds {ARCHIVE_MAX_BYTES} byte limit")
            self._bytes = source
        elif isinstance(source, Path):
            try:
                if source.stat().st_size > ARCHIVE_MAX_BYTES:
                    raise PackageSizeLimitExceeded(
                        f"archive exceeds {ARCHIVE_MAX_BYTES} byte limit"
                    )
            except OSError as error:
                raise PackageArchiveError("archive could not be inspected") from error
        else:
            if not source.seekable():
                raise PackageArchiveError("archive source must be seekable")
            current = source.tell()
            source.seek(0, io.SEEK_END)
            size = source.tell()
            source.seek(current)
            if size > ARCHIVE_MAX_BYTES:
                raise PackageSizeLimitExceeded(f"archive exceeds {ARCHIVE_MAX_BYTES} byte limit")
            self._stream = source

        self._members: dict[str, ArchiveMember] = {}
        self._infos: dict[str, zipfile.ZipInfo] = {}
        self._declared_total = 0
        try:
            with self._zip() as archive:
                self._scan(archive)
        except zipfile.BadZipFile as error:
            raise PackageArchiveError("archive is not a valid ZIP") from error

    @contextmanager
    def _zip(self) -> Generator[zipfile.ZipFile, None, None]:
        if self._path is not None:
            with zipfile.ZipFile(self._path) as archive:
                yield archive
            return
        if self._bytes is not None:
            with zipfile.ZipFile(io.BytesIO(self._bytes)) as archive:
                yield archive
            return
        assert self._stream is not None
        self._stream.seek(0)
        with zipfile.ZipFile(self._stream) as archive:
            yield archive

    def _scan(self, archive: zipfile.ZipFile) -> None:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise PackageSizeLimitExceeded(f"archive exceeds {MAX_ENTRIES} entry limit")

        claimed: dict[str, str] = {}
        for info in infos:
            path = canonical_package_path(info.filename)
            if info.flag_bits & 0x1:
                raise PackageArchiveError("encrypted archive members are not supported")
            if info.compress_type not in _allowed_compression(self._profile):
                raise PackageArchiveError("archive member uses unsupported compression")
            if info.is_dir():
                if self._profile is ArchiveValidationProfile.NERVOS_V1:
                    raise PackageArchiveError("outer archive must not contain directory entries")
                continue
            key = collision_key(path)
            if key in claimed:
                raise DuplicatePackageEntry(
                    f"archive contains colliding entries ({claimed[key]!r} and {path!r})"
                )
            claimed[key] = path

            if self._profile is ArchiveValidationProfile.NERVOS_V1:
                # Canonical metadata is a property of the outer NervOS V1 wire format only.
                if info.date_time != FIXED_DATE_TIME:
                    raise PackageArchiveError("archive member has a noncanonical timestamp")
                if info.create_system != FIXED_CREATE_SYSTEM:
                    raise PackageArchiveError("archive member has a noncanonical creator system")
                if info.external_attr != FIXED_EXTERNAL_ATTR:
                    raise PackageArchiveError("archive member has noncanonical external attributes")
                if info.internal_attr != FIXED_INTERNAL_ATTR:
                    raise PackageArchiveError("archive member has noncanonical internal attributes")
                if info.extra != FIXED_EXTRA or info.comment != FIXED_COMMENT:
                    raise PackageArchiveError("archive member has noncanonical extra metadata")
            else:
                _reject_unsafe_wheel_type(info)

            size = info.file_size
            if size > SINGLE_FILE_MAX_BYTES:
                raise PackageSizeLimitExceeded(
                    f"archive member exceeds {SINGLE_FILE_MAX_BYTES} byte limit"
                )
            self._declared_total += size
            if self._declared_total > EXTRACTED_MAX_BYTES:
                raise PackageSizeLimitExceeded(
                    f"declared archive contents exceed {EXTRACTED_MAX_BYTES} byte limit"
                )

            self._members[path] = ArchiveMember(
                path=path, size=size, compression=info.compress_type
            )
            self._infos[path] = info

    @property
    def total_declared_bytes(self) -> int:
        return self._declared_total

    def paths(self) -> tuple[str, ...]:
        """Every member path, in canonical order."""
        return canonical_order(self._members)

    def member(self, path: str) -> ArchiveMember | None:
        return self._members.get(path)

    def read(self, path: str) -> bytes:
        """Read one member's exact bytes, verifying the declared size while streaming.

        The declared size is a claim in the central directory, so the streamed byte count is what is
        enforced: reading aborts the moment more bytes appear than were declared, or more than the
        frozen per-file bound allows, before the excess is retained.
        """
        info = self._infos.get(path)
        if info is None:
            raise PackageArchiveError(f"archive member {path!r} does not exist")
        declared = info.file_size
        chunk_limit = min(declared, SINGLE_FILE_MAX_BYTES)
        try:
            with self._zip() as archive:
                pieces: list[bytes] = []
                seen = 0
                with archive.open(info) as handle:
                    while True:
                        chunk = handle.read(READ_CHUNK_BYTES)
                        if not chunk:
                            break
                        seen += len(chunk)
                        if seen > chunk_limit:
                            raise PackageSizeLimitExceeded(
                                "archive member contains more bytes than it declares"
                            )
                        pieces.append(chunk)
        except zipfile.BadZipFile as error:
            raise PackageArchiveError("archive member could not be decompressed") from error
        if seen != declared:
            raise PackageArchiveError("archive member is truncated")
        return b"".join(pieces)

    def stream(self, path: str) -> Iterator[bytes]:
        """Yield one member's bytes in bounded chunks, enforcing the same limits as `read`."""
        info = self._infos.get(path)
        if info is None:
            raise PackageArchiveError(f"archive member {path!r} does not exist")
        declared = info.file_size
        chunk_limit = min(declared, SINGLE_FILE_MAX_BYTES)
        seen = 0
        try:
            with self._zip() as archive, archive.open(info) as handle:
                while True:
                    chunk = handle.read(READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    seen += len(chunk)
                    if seen > chunk_limit:
                        raise PackageSizeLimitExceeded(
                            "archive member contains more bytes than it declares"
                        )
                    yield chunk
        except zipfile.BadZipFile as error:
            raise PackageArchiveError("archive member could not be decompressed") from error
        if seen != declared:
            raise PackageArchiveError("archive member is truncated")
