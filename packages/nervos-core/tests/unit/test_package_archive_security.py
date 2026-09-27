"""G2 malformed-archive and layout rejection matrix.

Every case here is an archive that a hostile or careless producer could ship. The requirement is
uniform: reject, and reject before doing expensive work.
"""

from __future__ import annotations

import io
import warnings
import zipfile

import pytest
from nervos_core.application.package_archive import (
    ARCHIVE_MAX_BYTES,
    EXTRACTED_MAX_BYTES,
    MAX_ENTRIES,
    ArchiveValidationProfile,
    BoundedArchiveReader,
    PackageArchiveError,
    canonical_zip_info,
)
from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
from nervos_core.application.package_integrity import (
    FILES_FORMAT_VERSION,
    canonical_json_bytes,
)
from nervos_core.application.package_paths import (
    DuplicatePackageEntry,
    PackageSizeLimitExceeded,
    UnsafePackagePath,
)
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_verification import verify_package
from package_fixtures import (
    TEST_SIGNING_SEED,
    VALID_CONFIG_SCHEMA,
    VALID_MANIFEST,
    valid_wheel,
)


def _signer() -> Ed25519PackageSigner:
    return Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED)


def _built() -> bytes:
    return package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=VALID_MANIFEST,
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            agent_wheel_bytes=valid_wheel(),
        ),
        signer=_signer(),
    )


def _members(archive: bytes) -> dict[str, bytes]:
    reader = BoundedArchiveReader(archive, profile=ArchiveValidationProfile.NERVOS_V1)
    return {path: reader.read(path) for path in reader.paths()}


def _repack(members: dict[str, bytes], *, stored: bool = True) -> bytes:
    buffer = io.BytesIO()
    compression = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        for path, payload in members.items():
            archive.writestr(canonical_zip_info(path, len(payload)), payload)
    return buffer.getvalue()


class TestMissingMembers:
    @pytest.mark.parametrize(
        "member",
        [
            "manifest.yaml",
            "agent.whl",
            "config.schema.json",
            "dependencies/lock.json",
            "integrity/files.json",
            "integrity/signature.json",
        ],
    )
    def test_missing_required_member_fails(self, member: str) -> None:
        members = _members(_built())
        del members[member]
        with pytest.raises(ValueError):
            verify_package(archive_bytes=_repack(members))


class TestUnexpectedMembers:
    @pytest.mark.parametrize(
        "member",
        [
            "extra.txt",
            ".env",
            "credentials.json",
            "secrets/token.txt",
            "migrations/0001_init.py",
            "nervos.db",
            "notes.log",
            "assets/../escape.txt",
            "integrity/extra.json",
            "dependencies/extra.txt",
            "dependencies/wheels/notawheel.txt",
        ],
    )
    def test_member_outside_the_layout_fails(self, member: str) -> None:
        members = _members(_built())
        members[member] = b"x"
        with pytest.raises((ValueError, UnsafePackagePath)):
            verify_package(archive_bytes=_repack(members))

    def test_package_owned_migrations_are_rejected(self) -> None:
        """ADR 0024 forbids package-owned SQL/Alembic migrations, and the layout enforces it."""
        members = _members(_built())
        members["migrations/0001_init.py"] = b"def upgrade(): pass\n"
        with pytest.raises(ValueError):
            verify_package(archive_bytes=_repack(members))


class TestUnsafePaths:
    @pytest.mark.parametrize(
        "path",
        [
            "../escape.txt",
            "/absolute.txt",
            "C:/drive.txt",
            "\\\\unc\\share.txt",
            "assets/../../escape.txt",
            "assets/./file.txt",
            "back\\slash.txt",
            "assets/..",
        ],
    )
    def test_traversal_and_absolute_paths_fail(self, path: str) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(canonical_zip_info(path, 1), b"x")
        with pytest.raises((UnsafePackagePath, PackageArchiveError, ValueError)):
            verify_package(archive_bytes=buffer.getvalue())


class TestDuplicateAndCollidingMembers:
    def test_duplicate_member_fails(self) -> None:
        # The duplicate name is the point of the fixture, so `zipfile`'s own warning about it is
        # suppressed rather than allowed to surface as a test-suite warning.
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr(canonical_zip_info("manifest.yaml", 1), b"a")
                archive.writestr(canonical_zip_info("manifest.yaml", 1), b"b")
        with pytest.raises((DuplicatePackageEntry, ValueError)):
            verify_package(archive_bytes=buffer.getvalue())

    def test_case_colliding_members_fail(self) -> None:
        members = _members(_built())
        members["README.md"] = b"# one\n"
        members["readme.md"] = b"# two\n"
        with pytest.raises((DuplicatePackageEntry, ValueError)):
            verify_package(archive_bytes=_repack(members))

    def test_directory_entry_fails(self) -> None:
        """The format has no directory entries, so one is a structural rejection."""
        members = _members(_built())
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(canonical_zip_info("assets/", 0), b"")
            for path, payload in members.items():
                archive.writestr(canonical_zip_info(path, len(payload)), payload)
        with pytest.raises((UnsafePackagePath, PackageArchiveError, ValueError)):
            verify_package(archive_bytes=buffer.getvalue())

    def test_symlink_entry_fails(self) -> None:
        members = _members(_built())
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            info = canonical_zip_info("link.txt", 4)
            info.external_attr = 0o120777 << 16
            archive.writestr(info, b"/etc")
            for path, payload in members.items():
                archive.writestr(canonical_zip_info(path, len(payload)), payload)
        with pytest.raises((PackageArchiveError, ValueError)):
            verify_package(archive_bytes=buffer.getvalue())

    def test_device_entry_fails(self) -> None:
        members = _members(_built())
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            info = canonical_zip_info("dev0", 1)
            info.external_attr = 0o020666 << 16
            archive.writestr(info, b"x")
            for path, payload in members.items():
                archive.writestr(canonical_zip_info(path, len(payload)), payload)
        with pytest.raises((PackageArchiveError, ValueError)):
            verify_package(archive_bytes=buffer.getvalue())


class TestSizeLimits:
    def test_archive_over_the_limit_fails(self) -> None:
        with pytest.raises(PackageSizeLimitExceeded):
            verify_package(archive_bytes=b"\0" * (ARCHIVE_MAX_BYTES + 1))

    def test_entry_count_over_the_limit_fails(self) -> None:
        members = {f"assets-{index}.txt": b"x" for index in range(MAX_ENTRIES + 1)}
        with pytest.raises(PackageSizeLimitExceeded):
            verify_package(archive_bytes=_repack(members))

    def test_declared_extracted_total_over_the_limit_fails(self) -> None:
        """A ZIP bomb is refused from declared metadata, before anything is decompressed."""
        members = _members(_built())
        buffer = io.BytesIO()
        per_member = EXTRACTED_MAX_BYTES // 4
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            for index in range(6):
                info = canonical_zip_info(f"bomb-{index}.bin", per_member)
                archive.writestr(info, b"")
            for path, payload in members.items():
                archive.writestr(canonical_zip_info(path, len(payload)), payload)
        with pytest.raises((PackageSizeLimitExceeded, ValueError)):
            verify_package(archive_bytes=_repack(members) + buffer.getvalue())

    def test_highly_compressed_member_over_its_budget_fails(self) -> None:
        """A member that decompresses far beyond its declared size is caught while streaming."""
        members = _members(_built())
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            # Declared small, contains a large run of zeroes.
            info = zipfile.ZipInfo(filename="manifest.yaml", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, b"\0" * (1024 * 1024))
            for path, payload in members.items():
                if path != "manifest.yaml":
                    archive.writestr(canonical_zip_info(path, len(payload)), payload)
        payload = buffer.getvalue()
        with pytest.raises(ValueError):
            verify_package(archive_bytes=payload)


class TestMalformedArchives:
    def test_not_a_zip_fails(self) -> None:
        with pytest.raises(PackageArchiveError):
            verify_package(archive_bytes=b"definitely not a zip file")

    def test_truncated_archive_fails(self) -> None:
        """A truncated outer archive is rejected.

        Reserving comment for a real subtlety this case exposed: because payloads are stored
        uncompressed, the embedded `agent.whl` carries its own end-of-central-directory record. When
        the outer one is cut off, `zipfile` can legitimately find the *inner* record and report the
        wheel's internal members as the archive's entries. That is not a hole -- the frozen layout
        allowlist then rejects those members for being outside the V1 layout -- but it is why this
        assertion is `ValueError` rather than one specific archive error.
        """
        archive = _built()
        with pytest.raises(ValueError):
            verify_package(archive_bytes=archive[: len(archive) // 2])

    def test_empty_input_fails(self) -> None:
        with pytest.raises(PackageArchiveError):
            verify_package(archive_bytes=b"")

    def test_central_directory_garbage_fails(self) -> None:
        archive = bytearray(_built())
        archive[-10:] = b"\x00" * 10
        with pytest.raises((PackageArchiveError, ValueError)):
            verify_package(archive_bytes=bytes(archive))


class TestLayoutConsistency:
    def test_assets_must_match_the_manifest_declaration(self) -> None:
        manifest = VALID_MANIFEST.replace(
            b"configuration:\n  schema: config.schema.json\n",
            b"configuration:\n  schema: config.schema.json\nassets:\n  - assets/declared.txt\n",
        )
        members = _members(
            package_build_bytes(
                PackageBuildInputs(
                    manifest_bytes=manifest,
                    config_schema_bytes=VALID_CONFIG_SCHEMA,
                    agent_wheel_bytes=valid_wheel(),
                    assets={"assets/declared.txt": b"x"},
                ),
                signer=_signer(),
            )
        )
        # Remove the declared asset: the manifest still claims it.
        del members["assets/declared.txt"]
        with pytest.raises(ValueError):
            verify_package(archive_bytes=_repack(members))

    def test_undeclared_asset_in_the_archive_fails(self) -> None:
        members = _members(_built())
        members["assets/undeclared.txt"] = b"x"
        with pytest.raises(ValueError):
            verify_package(archive_bytes=_repack(members))

    def test_config_schema_reference_must_be_the_frozen_member(self) -> None:
        members = _members(_built())
        members["config.schema.json"] = b'{"type": "object"}'
        with pytest.raises(ValueError):
            verify_package(archive_bytes=_repack(members))

    def test_unsupported_files_format_version_fails(self) -> None:
        members = _members(_built())
        import json

        document = json.loads(members["integrity/files.json"].decode())
        document["files_format_version"] = FILES_FORMAT_VERSION + "9"
        members["integrity/files.json"] = canonical_json_bytes(document)
        with pytest.raises(ValueError):
            verify_package(archive_bytes=_repack(members))

    def test_non_canonical_files_json_fails(self) -> None:
        import json

        members = _members(_built())
        document = json.loads(members["integrity/files.json"].decode())
        members["integrity/files.json"] = json.dumps(document, indent=2).encode()
        with pytest.raises(ValueError):
            verify_package(archive_bytes=_repack(members))
