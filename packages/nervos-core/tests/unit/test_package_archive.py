"""G2 canonical ZIP container behaviour and bounded archive reading.

The container tests assert the properties that make a `.nervos` archive byte-identical across
machines: fixed timestamps, fixed attributes, a fixed creator system, stored (not deflated)
members, canonical ordering, and no directory entries. The reader tests assert that limits are
enforced against the *streamed* bytes rather than against declared metadata.
"""

from __future__ import annotations

import io
import warnings
import zipfile

import pytest
from nervos_core.application.package_archive import (
    ARCHIVE_MAX_BYTES,
    FIXED_CREATE_SYSTEM,
    FIXED_DATE_TIME,
    FIXED_EXTERNAL_ATTR,
    MAX_ENTRIES,
    SINGLE_FILE_MAX_BYTES,
    ArchiveValidationProfile,
    BoundedArchiveReader,
    PackageArchiveError,
    canonical_zip_info,
    write_canonical_archive,
)
from nervos_core.application.package_paths import (
    DuplicatePackageEntry,
    PackageSizeLimitExceeded,
    UnsafePackagePath,
)


def _with_declared_size(raw: bytes, *, declared: int) -> bytes:
    """Forge the declared uncompressed size of the first member in a stored ZIP.

    Stored members have no compressed payload between the local header and the data, so patching
    both the local header and the central directory produces a consistent archive that *claims* a
    size it does not contain -- exactly the input the scan-time limit must catch.
    """
    patched = bytearray(raw)
    local_size_offset = 22  # local file header: uncompressed size field
    patched[local_size_offset : local_size_offset + 4] = declared.to_bytes(4, "little")
    central = patched.rfind(b"PK\x01\x02")
    patched[central + 24 : central + 28] = declared.to_bytes(4, "little")
    return bytes(patched)


class TestCanonicalWriter:
    def test_metadata_is_fixed_not_host_derived(self) -> None:
        raw = write_canonical_archive({"manifest.yaml": b"content\n"})
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            info = archive.infolist()[0]
        assert info.date_time == FIXED_DATE_TIME
        assert info.create_system == FIXED_CREATE_SYSTEM
        assert info.external_attr == FIXED_EXTERNAL_ATTR
        assert info.compress_type == zipfile.ZIP_STORED
        assert info.extra == b""
        assert info.comment == b""

    def test_outer_windows_creator_is_rejected(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            info = canonical_zip_info("manifest.yaml", 1)
            info.create_system = 0
            archive.writestr(info, b"x")
        with pytest.raises(PackageArchiveError, match="creator system"):
            BoundedArchiveReader(buffer.getvalue(), profile=ArchiveValidationProfile.NERVOS_V1)

    def test_outer_deflated_member_is_rejected(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            info = canonical_zip_info("manifest.yaml", 1)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, b"x")
        with pytest.raises(PackageArchiveError, match="compression"):
            BoundedArchiveReader(buffer.getvalue(), profile=ArchiveValidationProfile.NERVOS_V1)

    def test_utf8_flag_is_set(self) -> None:
        raw = write_canonical_archive({"assets/ünïcode.txt": b"x"})
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            info = archive.infolist()[0]
        assert info.flag_bits & 0x800

    def test_entries_are_canonically_ordered(self) -> None:
        raw = write_canonical_archive({"b.txt": b"b", "a.txt": b"a", "c.txt": b"c"})
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            assert archive.namelist() == ["a.txt", "b.txt", "c.txt"]

    def test_ordering_is_independent_of_mapping_order(self) -> None:
        first = write_canonical_archive({"b.txt": b"b", "a.txt": b"a", "c.txt": b"c"})
        second = write_canonical_archive({"c.txt": b"c", "a.txt": b"a", "b.txt": b"b"})
        assert first == second

    def test_no_directory_entries_are_emitted(self) -> None:
        raw = write_canonical_archive({"assets/deep/file.txt": b"x"})
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            assert archive.namelist() == ["assets/deep/file.txt"]

    def test_payloads_are_stored_unrewritten(self) -> None:
        """Payload bytes are exact: never re-compressed, re-encoded, or newline-normalized."""
        payload = bytes(range(256)) * 4
        raw = write_canonical_archive({"agent.whl": payload})
        reader = BoundedArchiveReader(raw, profile=ArchiveValidationProfile.NERVOS_V1)
        assert reader.read("agent.whl") == payload

    def test_builds_are_byte_identical(self) -> None:
        entries = {"manifest.yaml": b"m", "assets/a.txt": b"a"}
        assert write_canonical_archive(entries) == write_canonical_archive(dict(entries))


class TestBoundedReader:
    def test_reads_every_member(self) -> None:
        raw = write_canonical_archive({"a.txt": b"alpha", "b.txt": b"beta"})
        reader = BoundedArchiveReader(raw, profile=ArchiveValidationProfile.NERVOS_V1)
        assert reader.paths() == ("a.txt", "b.txt")
        assert reader.read("a.txt") == b"alpha"
        assert reader.total_declared_bytes == 9

    def test_missing_member_raises(self) -> None:
        reader = BoundedArchiveReader(
            write_canonical_archive({"a.txt": b"a"}), profile=ArchiveValidationProfile.NERVOS_V1
        )
        with pytest.raises(PackageArchiveError):
            reader.read("missing.txt")

    def test_non_zip_input_is_rejected(self) -> None:
        with pytest.raises(PackageArchiveError):
            BoundedArchiveReader(
                b"this is not a zip archive at all", profile=ArchiveValidationProfile.NERVOS_V1
            )

    def test_truncated_archive_is_rejected(self) -> None:
        raw = write_canonical_archive({"a.txt": b"a" * 5000})
        with pytest.raises(PackageArchiveError):
            BoundedArchiveReader(raw[: len(raw) // 2], profile=ArchiveValidationProfile.NERVOS_V1)

    def test_oversized_archive_is_rejected_before_parsing(self) -> None:
        with pytest.raises(PackageSizeLimitExceeded):
            BoundedArchiveReader(
                b"\0" * (ARCHIVE_MAX_BYTES + 1),
                profile=ArchiveValidationProfile.NERVOS_V1,
            )

    def test_entry_count_limit_is_enforced(self) -> None:
        entries = {f"file-{index}.txt": b"x" for index in range(MAX_ENTRIES + 1)}
        raw = write_canonical_archive(entries)
        with pytest.raises(PackageSizeLimitExceeded):
            BoundedArchiveReader(raw, profile=ArchiveValidationProfile.NERVOS_V1)

    def test_single_file_limit_is_enforced(self) -> None:
        """Declared sizes are checked at scan time, before anything is decompressed.

        `ZipFile.writestr` overwrites the declared size with the real one, so the oversized
        declaration is forged by patching the central directory and local header directly -- which
        is also how a hostile archive would present it.
        """
        raw = _with_declared_size(
            write_canonical_archive({"big.bin": b"small"}),
            declared=SINGLE_FILE_MAX_BYTES + 1,
        )
        with pytest.raises(PackageSizeLimitExceeded):
            BoundedArchiveReader(raw, profile=ArchiveValidationProfile.NERVOS_V1)

    def test_unsafe_member_path_is_rejected(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(canonical_zip_info("../escape.txt", 1), b"x")
        with pytest.raises(UnsafePackagePath):
            BoundedArchiveReader(buffer.getvalue(), profile=ArchiveValidationProfile.NERVOS_V1)

    def test_duplicate_member_is_rejected(self) -> None:
        # `zipfile` warns when the same name is written twice; the archive we are constructing is
        # deliberately that archive, so the warning is expected rather than meaningful.
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr(canonical_zip_info("dup.txt", 1), b"a")
                archive.writestr(canonical_zip_info("dup.txt", 1), b"b")
        with pytest.raises(DuplicatePackageEntry):
            BoundedArchiveReader(buffer.getvalue(), profile=ArchiveValidationProfile.NERVOS_V1)

    def test_case_colliding_members_are_rejected(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(canonical_zip_info("Foo.txt", 1), b"a")
            archive.writestr(canonical_zip_info("foo.txt", 1), b"b")
        with pytest.raises(DuplicatePackageEntry):
            BoundedArchiveReader(buffer.getvalue(), profile=ArchiveValidationProfile.NERVOS_V1)

    def test_directory_entries_are_rejected(self) -> None:
        """A ZIP directory member (empty name component, trailing separator) must not be accepted.

        These are hand-built because the canonical writer never emits them; the reader must still
        refuse them, since an archive does not have to come from this writer.
        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(canonical_zip_info("assets/", 0), b"")
            archive.writestr(canonical_zip_info("assets/a.txt", 1), b"a")
        with pytest.raises((UnsafePackagePath, PackageArchiveError)):
            BoundedArchiveReader(buffer.getvalue(), profile=ArchiveValidationProfile.NERVOS_V1)

    def test_symlink_entry_is_rejected(self) -> None:
        """A symlink is recorded as a non-regular-file type in the high mode bits."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            info = canonical_zip_info("link.txt", 4)
            info.external_attr = 0o120777 << 16  # S_IFLNK
            archive.writestr(info, b"/etc")
        with pytest.raises(PackageArchiveError):
            BoundedArchiveReader(buffer.getvalue(), profile=ArchiveValidationProfile.NERVOS_V1)

    def test_device_entry_is_rejected(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            info = canonical_zip_info("dev", 1)
            info.external_attr = 0o020666 << 16  # S_IFCHR
            archive.writestr(info, b"x")
        with pytest.raises(PackageArchiveError):
            BoundedArchiveReader(buffer.getvalue(), profile=ArchiveValidationProfile.NERVOS_V1)

    def test_stream_yields_same_bytes_as_read(self) -> None:
        payload = b"streamed payload" * 100
        reader = BoundedArchiveReader(
            write_canonical_archive({"a.bin": payload}), profile=ArchiveValidationProfile.NERVOS_V1
        )
        assert b"".join(reader.stream("a.bin")) == payload

    def test_member_reports_declared_size(self) -> None:
        reader = BoundedArchiveReader(
            write_canonical_archive({"a.bin": b"12345"}), profile=ArchiveValidationProfile.NERVOS_V1
        )
        member = reader.member("a.bin")
        assert member is not None
        assert member.size == 5
