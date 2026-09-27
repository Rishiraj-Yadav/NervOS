"""G2 tamper detection: every meaningful mutation of a built package must fail verification.

Verification is all-or-nothing. There is no partial acceptance, no "mostly verified", and no
warning-then-continue path: each case below mutates exactly one thing and requires a rejection.
"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from typing import Any

import pytest
from nervos_core.application.package_archive import (
    ArchiveValidationProfile,
    BoundedArchiveReader,
)
from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
from nervos_core.application.package_integrity import canonical_json_bytes
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_verification import verify_package
from package_fixtures import (
    TEST_SIGNING_SEED,
    VALID_CONFIG_SCHEMA,
    VALID_MANIFEST,
    valid_dependency_wheel,
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
            readme_bytes=b"# Acme\n",
            dependency_wheels={"helper_lib-2.0.0-py3-none-any.whl": valid_dependency_wheel()},
        ),
        signer=_signer(),
    )


def _rewrite(archive: bytes, replacements: dict[str, bytes]) -> bytes:
    """Replace or add member payloads, preserving the metadata of entries that already existed.

    New members are appended so an "unexpected extra file" case is genuinely represented rather
    than quietly dropped.
    """
    with zipfile.ZipFile(io.BytesIO(archive)) as original:
        infos = original.infolist()
        payloads = {info.filename: original.read(info.filename) for info in infos}
    payloads.update(replacements)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as rebuilt:
        for info in infos:
            name = info.filename
            if name not in payloads:
                continue
            new_info = zipfile.ZipInfo(filename=name, date_time=info.date_time)
            new_info.compress_type = zipfile.ZIP_STORED
            new_info.create_system = info.create_system
            new_info.external_attr = info.external_attr
            rebuilt.writestr(new_info, payloads.pop(name))
        for name, payload in payloads.items():
            new_info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
            new_info.compress_type = zipfile.ZIP_STORED
            rebuilt.writestr(new_info, payload)
    return buffer.getvalue()


class TestPayloadTampering:
    """Every payload mutation must be caught, whatever part of the package it touches."""

    @pytest.mark.parametrize(
        "member",
        [
            "manifest.yaml",
            "config.schema.json",
            "agent.whl",
            "README.md",
            "dependencies/lock.json",
            "dependencies/wheels/helper_lib-2.0.0-py3-none-any.whl",
        ],
    )
    def test_payload_byte_change_fails(self, member: str) -> None:
        archive = _built()
        reader = BoundedArchiveReader(archive, profile=ArchiveValidationProfile.NERVOS_V1)
        original = reader.read(member)
        tampered = _rewrite(archive, {member: original + b"\x00"})
        with pytest.raises(ValueError):
            verify_package(archive_bytes=tampered)

    def test_manifest_content_change_fails(self) -> None:
        """A semantically meaningful manifest edit is caught by the digest, not only by parsing."""
        archive = _built()
        tampered = _rewrite(
            archive,
            {"manifest.yaml": VALID_MANIFEST.replace(b"display_name: Acme", b"display_name: Evil")},
        )
        with pytest.raises(ValueError):
            verify_package(archive_bytes=tampered)

    def test_added_member_fails(self) -> None:
        archive = _built()
        tampered = _rewrite(archive, {"assets/extra.txt": b"surprise"})
        with pytest.raises(ValueError):
            verify_package(archive_bytes=tampered)


class TestIntegrityManifestTampering:
    def _manifest_document(self, archive: bytes) -> dict[str, Any]:
        reader = BoundedArchiveReader(archive, profile=ArchiveValidationProfile.NERVOS_V1)
        return json.loads(reader.read("integrity/files.json").decode())

    def test_files_json_digest_change_fails(self) -> None:
        archive = _built()
        document = self._manifest_document(archive)
        document["files"][0]["sha256"] = "0" * 64
        tampered = _rewrite(archive, {"integrity/files.json": canonical_json_bytes(document)})
        with pytest.raises(ValueError):
            verify_package(archive_bytes=tampered)

    def test_files_json_size_change_fails(self) -> None:
        archive = _built()
        document = self._manifest_document(archive)
        document["files"][0]["size"] = 999999
        tampered = _rewrite(archive, {"integrity/files.json": canonical_json_bytes(document)})
        with pytest.raises(ValueError):
            verify_package(archive_bytes=tampered)

    def test_files_json_dropped_entry_fails(self) -> None:
        archive = _built()
        document = self._manifest_document(archive)
        document["files"] = document["files"][1:]
        tampered = _rewrite(archive, {"integrity/files.json": canonical_json_bytes(document)})
        with pytest.raises(ValueError):
            verify_package(archive_bytes=tampered)

    def test_files_json_reordering_fails(self) -> None:
        """Reordering changes the canonical bytes, so it changes the digest and is rejected."""
        archive = _built()
        document = self._manifest_document(archive)
        document["files"] = list(reversed(document["files"]))
        tampered = _rewrite(archive, {"integrity/files.json": canonical_json_bytes(document)})
        with pytest.raises(ValueError):
            verify_package(archive_bytes=tampered)

    def test_files_json_self_listing_fails(self) -> None:
        archive = _built()
        document = self._manifest_document(archive)
        document["files"].append({"path": "integrity/files.json", "sha256": "a" * 64, "size": 1})
        tampered = _rewrite(archive, {"integrity/files.json": canonical_json_bytes(document)})
        with pytest.raises(ValueError):
            verify_package(archive_bytes=tampered)


class TestSignatureTampering:
    def _document(self, archive: bytes) -> dict[str, Any]:
        reader = BoundedArchiveReader(archive, profile=ArchiveValidationProfile.NERVOS_V1)
        return json.loads(reader.read("integrity/signature.json").decode())

    def test_signature_bytes_change_fails(self) -> None:
        archive = _built()
        document = self._document(archive)
        raw = bytearray(base64.b64decode(document["signature"]))
        raw[0] ^= 0xFF
        document["signature"] = base64.b64encode(bytes(raw)).decode()
        tampered = _rewrite(archive, {"integrity/signature.json": canonical_json_bytes(document)})
        with pytest.raises(ValueError):
            verify_package(archive_bytes=tampered)

    def test_public_key_change_fails(self) -> None:
        archive = _built()
        document = self._document(archive)
        other = Ed25519PackageSigner.from_private_bytes(bytes(range(32, 64)))
        document["public_key"] = base64.b64encode(other.public_key_bytes()).decode()
        tampered = _rewrite(archive, {"integrity/signature.json": canonical_json_bytes(document)})
        with pytest.raises(ValueError):
            verify_package(archive_bytes=tampered)

    def test_fingerprint_change_fails(self) -> None:
        archive = _built()
        document = self._document(archive)
        document["key_fingerprint"] = "0" * 64
        tampered = _rewrite(archive, {"integrity/signature.json": canonical_json_bytes(document)})
        with pytest.raises(ValueError):
            verify_package(archive_bytes=tampered)

    def test_algorithm_change_fails(self) -> None:
        archive = _built()
        document = self._document(archive)
        document["algorithm"] = "rsa"
        tampered = _rewrite(archive, {"integrity/signature.json": canonical_json_bytes(document)})
        with pytest.raises(ValueError):
            verify_package(archive_bytes=tampered)

    def test_format_version_change_fails(self) -> None:
        archive = _built()
        document = self._document(archive)
        document["signature_format_version"] = "2"
        tampered = _rewrite(archive, {"integrity/signature.json": canonical_json_bytes(document)})
        with pytest.raises(ValueError):
            verify_package(archive_bytes=tampered)

    def test_missing_signature_fails(self) -> None:
        """Signatures are mandatory, so removing one is a rejection rather than an unsigned mode."""
        archive = _built()
        with zipfile.ZipFile(io.BytesIO(archive)) as original:
            infos = original.infolist()
            payloads = [
                (info, original.read(info.filename))
                for info in infos
                if info.filename != "integrity/signature.json"
            ]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as rebuilt:
            for info, payload in payloads:
                rebuilt.writestr(info, payload)
        with pytest.raises(ValueError):
            verify_package(archive_bytes=buffer.getvalue())


class TestEnvelopeTampering:
    """The envelope is rebuilt from verified state, so it cannot be edited independently.

    These cases mutate the *signed inputs* instead, which is the only way to attack the envelope.
    """

    def test_envelope_package_id_cannot_be_forged(self) -> None:
        """Changing the package id changes the envelope and invalidates the signature."""
        changed_manifest = VALID_MANIFEST.replace(
            b"package_id: com.acme.invoice", b"package_id: com.evil.invoice"
        )
        archive = package_build_bytes(
            PackageBuildInputs(
                manifest_bytes=changed_manifest,
                config_schema_bytes=VALID_CONFIG_SCHEMA,
                agent_wheel_bytes=valid_wheel(),
            ),
            signer=_signer(),
        )
        # Signed correctly for its own identity, so it verifies...
        assert verify_package(archive_bytes=archive).manifest.package_id == "com.evil.invoice"

        # ...but transplanting that signature onto the original payload must fail.
        original = _built()
        document = json.loads(
            BoundedArchiveReader(archive, profile=ArchiveValidationProfile.NERVOS_V1)
            .read("integrity/signature.json")
            .decode()
        )
        tampered = _rewrite(original, {"integrity/signature.json": canonical_json_bytes(document)})
        with pytest.raises(ValueError):
            verify_package(archive_bytes=tampered)

    def test_envelope_package_version_cannot_be_forged(self) -> None:
        archive = package_build_bytes(
            PackageBuildInputs(
                manifest_bytes=VALID_MANIFEST.replace(
                    b"package_version: 1.2.3", b"package_version: 9.9.9"
                ),
                config_schema_bytes=VALID_CONFIG_SCHEMA,
                agent_wheel_bytes=valid_wheel(),
            ),
            signer=_signer(),
        )
        document = json.loads(
            BoundedArchiveReader(archive, profile=ArchiveValidationProfile.NERVOS_V1)
            .read("integrity/signature.json")
            .decode()
        )
        tampered = _rewrite(_built(), {"integrity/signature.json": canonical_json_bytes(document)})
        with pytest.raises(ValueError):
            verify_package(archive_bytes=tampered)

    def test_content_digest_cannot_be_forged(self) -> None:
        """A signature over a different content digest does not transfer to this payload."""
        other = package_build_bytes(
            PackageBuildInputs(
                manifest_bytes=VALID_MANIFEST,
                config_schema_bytes=VALID_CONFIG_SCHEMA.replace(b"hello", b"other"),
                agent_wheel_bytes=valid_wheel(),
            ),
            signer=_signer(),
        )
        document = json.loads(
            BoundedArchiveReader(other, profile=ArchiveValidationProfile.NERVOS_V1)
            .read("integrity/signature.json")
            .decode()
        )
        tampered = _rewrite(_built(), {"integrity/signature.json": canonical_json_bytes(document)})
        with pytest.raises(ValueError):
            verify_package(archive_bytes=tampered)


class TestBaselineIsValid:
    def test_the_untampered_package_verifies(self) -> None:
        """Every tamper test above is only meaningful if the original verifies."""
        verified = verify_package(archive_bytes=_built())
        assert verified.manifest.package_id == "com.acme.invoice"
        assert len(verified.entries) == 6

    def test_tamper_tests_do_not_pass_vacuously(self) -> None:
        archive = _built()
        assert verify_package(archive_bytes=archive).content_digest
        modified = _rewrite(archive, {"README.md": b"# Different\n"})
        assert modified != archive
