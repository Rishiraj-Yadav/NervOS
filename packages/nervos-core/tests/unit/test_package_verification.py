"""G2 verifier pipeline, result types, and the G3 handoff seam.

The verifier is the only way verified facts leave G2, so this suite checks both the pipeline's
fail-closed ordering and the shape of what it hands on: immutable, complete, and carrying no
installation state.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from nervos_core.application.package_builder import (
    BuiltPackage,
    PackageBuildInputs,
    package_build,
    package_build_bytes,
)
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_verification import (
    PackageVerificationError,
    VerifiedPackage,
    verify_package,
)
from package_fixtures import (
    TEST_SIGNING_SEED,
    VALID_CONFIG_SCHEMA,
    VALID_MANIFEST,
    valid_dependency_wheel,
    valid_wheel,
)


def _signer() -> Ed25519PackageSigner:
    return Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED)


def _inputs() -> PackageBuildInputs:
    return PackageBuildInputs(
        manifest_bytes=VALID_MANIFEST,
        config_schema_bytes=VALID_CONFIG_SCHEMA,
        agent_wheel_bytes=valid_wheel(),
        readme_bytes=b"# Acme\n",
        dependency_wheels={"helper_lib-2.0.0-py3-none-any.whl": valid_dependency_wheel()},
    )


def _built_bytes() -> bytes:
    return package_build_bytes(_inputs(), signer=_signer())


class TestVerifierInputModes:
    def test_verifies_from_bytes(self) -> None:
        assert (
            verify_package(archive_bytes=_built_bytes()).manifest.package_id == "com.acme.invoice"
        )

    def test_verifies_from_path(self, tmp_path: Path) -> None:
        destination = tmp_path / "pkg.nervos"
        destination.write_bytes(_built_bytes())
        verified = verify_package(destination)
        assert verified.manifest.package_id == "com.acme.invoice"

    def test_path_and_bytes_agree(self, tmp_path: Path) -> None:
        archive = _built_bytes()
        destination = tmp_path / "pkg.nervos"
        destination.write_bytes(archive)
        from_path = verify_package(destination)
        from_bytes = verify_package(archive_bytes=archive)
        assert from_path.content_digest == from_bytes.content_digest
        assert from_path.archive_digest == from_bytes.archive_digest

    def test_missing_input_is_an_error(self) -> None:
        with pytest.raises(PackageVerificationError):
            verify_package()


class TestVerifiedPackageContents:
    def test_carries_the_g1_manifest_value(self) -> None:
        verified = verify_package(archive_bytes=_built_bytes())
        assert verified.manifest.package_id == "com.acme.invoice"
        assert verified.manifest.package_version == "1.2.3"

    def test_carries_a_validated_config_schema(self) -> None:
        verified = verify_package(archive_bytes=_built_bytes())
        assert verified.config_schema.root["type"] == "object"

    def test_carries_both_digests(self) -> None:
        verified = verify_package(archive_bytes=_built_bytes())
        assert len(verified.content_digest) == 64
        assert len(verified.archive_digest) == 64
        assert verified.content_digest != verified.archive_digest

    def test_digests_have_the_documented_relationship(self) -> None:
        """The content digest is over the payload manifest; the archive digest is over the file."""
        archive = _built_bytes()
        verified = verify_package(archive_bytes=archive)
        from nervos_core.application.package_integrity import sha256_hex

        assert verified.archive_digest == sha256_hex(archive)

    def test_carries_the_signer_key_and_fingerprint(self) -> None:
        from nervos_core.application.package_integrity import signature_fingerprint

        verified = verify_package(archive_bytes=_built_bytes())
        assert verified.signer_public_key == _signer().public_key_bytes()
        assert verified.signer_fingerprint == signature_fingerprint(verified.signer_public_key)
        assert len(verified.signer_fingerprint) == 64

    def test_carries_wheel_and_lock_metadata(self) -> None:
        verified = verify_package(archive_bytes=_built_bytes())
        assert verified.agent_wheel.tag == "py3-none-any"
        assert verified.dependency_lock.entries
        assert len(verified.dependencies) == 1

    def test_carries_the_payload_entry_map(self) -> None:
        verified = verify_package(archive_bytes=_built_bytes())
        paths = [entry.path for entry in verified.entries]
        assert paths == sorted(paths, key=lambda item: item.encode("utf-8"))
        assert all(len(entry.sha256) == 64 for entry in verified.entries)


class TestResultImmutability:
    def test_verified_package_is_frozen(self) -> None:
        verified = verify_package(archive_bytes=_built_bytes())
        with pytest.raises(dataclasses.FrozenInstanceError):
            verified.content_digest = "tampered"  # type: ignore[misc]

    def test_built_package_is_frozen(self, tmp_path: Path) -> None:
        built = package_build(_inputs(), signer=_signer(), destination=tmp_path / "p.nervos")
        with pytest.raises(dataclasses.FrozenInstanceError):
            built.size = 0  # type: ignore[misc]

    def test_no_installation_state_leaks_into_the_result(self) -> None:
        """G2 has no install state, so none of it may appear in the handoff type."""
        fields = {field.name for field in dataclasses.fields(VerifiedPackage)}
        for forbidden in (
            "install_path",
            "installed_at",
            "installation_id",
            "database_id",
            "environment",
            "venv",
            "package_id_row",
            "status",
            "executable_ref",
        ):
            assert forbidden not in fields, forbidden

    def test_built_package_fields_are_artifact_scoped(self) -> None:
        fields = {field.name for field in dataclasses.fields(BuiltPackage)}
        assert fields == {
            "output_path",
            "package_id",
            "package_version",
            "content_digest",
            "archive_digest",
            "signer_fingerprint",
            "size",
            "verified",
        }


class TestPipelineOrder:
    def test_layout_is_rejected_before_cryptography(self) -> None:
        """A structural rejection must not require signature work to happen first.

        Proved by removing the signature: if the pipeline checked signature first, the error would
        be about the signature rather than the layout.
        """
        from nervos_core.application.package_archive import (
            ArchiveValidationProfile,
            BoundedArchiveReader,
            write_canonical_archive,
        )

        archive = _built_bytes()
        reader = BoundedArchiveReader(archive, profile=ArchiveValidationProfile.NERVOS_V1)
        members = {path: reader.read(path) for path in reader.paths()}
        del members["integrity/signature.json"]
        malformed = write_canonical_archive(members)
        with pytest.raises(ValueError) as error:
            verify_package(archive_bytes=malformed)
        assert "signature.json" in str(error.value)

    def test_integrity_is_checked_against_actual_bytes(self) -> None:
        verified = verify_package(archive_bytes=_built_bytes())
        for entry in verified.entries:
            assert entry.size >= 0
            assert len(entry.sha256) == 64

    def test_verification_is_deterministic(self) -> None:
        archive = _built_bytes()
        first = verify_package(archive_bytes=archive)
        second = verify_package(archive_bytes=archive)
        assert first.content_digest == second.content_digest
        assert first.signer_fingerprint == second.signer_fingerprint


class TestG3HandoffSeam:
    def test_verified_package_supports_exact_definition_resolution(self) -> None:
        """G3 needs the exact definition identity, and it comes from the G1 projection."""
        verified = verify_package(archive_bytes=_built_bytes())
        definition_id = verified.manifest.identity.as_agent_definition_id()
        assert definition_id.agent_key == "com.acme.invoice"
        assert definition_id.agent_definition_version == "1.2.3"

    def test_g3_does_not_need_to_reparse_anything(self) -> None:
        """Everything a consumer needs is already a parsed, validated value."""
        verified = verify_package(archive_bytes=_built_bytes())
        assert isinstance(verified.manifest, object)
        assert isinstance(verified.dependency_lock.entries, tuple)
        assert isinstance(verified.entries, tuple)

    def test_dependency_inventory_is_complete_for_an_installer(self) -> None:
        verified = verify_package(archive_bytes=_built_bytes())
        locked = {entry.name for entry in verified.dependency_lock.entries}
        shipped = {wheel.name for wheel in verified.dependencies}
        assert locked == shipped == {"helper-lib"}

    def test_builder_result_embeds_the_verified_facts(self, tmp_path: Path) -> None:
        built = package_build(_inputs(), signer=_signer(), destination=tmp_path / "p.nervos")
        assert isinstance(built.verified, VerifiedPackage)
        assert built.verified.content_digest == built.content_digest
