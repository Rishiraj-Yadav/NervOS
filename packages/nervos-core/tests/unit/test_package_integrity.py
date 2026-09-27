"""G2 integrity manifest, content digest, and canonical JSON behaviour.

The non-recursion rule gets its own tests here, because it is the one property of the integrity
model that would make the digest impossible to compute if it were violated rather than merely wrong:
`files.json` cannot contain its own hash.
"""

from __future__ import annotations

import json

import pytest
from nervos_core.application.package_integrity import (
    FILES_FORMAT_VERSION,
    INTEGRITY_FILES_PATH,
    INTEGRITY_SIGNATURE_PATH,
    MalformedIntegrityManifest,
    PackageIntegrityMismatch,
    build_content_manifest,
    build_signed_envelope,
    canonical_json_bytes,
    content_digest,
    normalized_distribution_name,
    normalized_distribution_version,
    parse_content_manifest,
    sha256_hex,
    signature_fingerprint,
    verify_payload_against_manifest,
)


class TestCanonicalJson:
    def test_keys_are_sorted(self) -> None:
        assert canonical_json_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'

    def test_separators_are_compact(self) -> None:
        assert b", " not in canonical_json_bytes({"a": [1, 2, 3]})

    def test_non_ascii_is_preserved(self) -> None:
        assert canonical_json_bytes({"a": "café"}) == '{"a":"café"}'.encode()

    def test_float_nan_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            canonical_json_bytes({"a": float("nan")})

    def test_float_infinity_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            canonical_json_bytes({"a": float("inf")})

    def test_encoding_is_deterministic_across_key_order(self) -> None:
        first = canonical_json_bytes({"z": 1, "a": {"y": 2, "b": 3}})
        second = canonical_json_bytes({"a": {"b": 3, "y": 2}, "z": 1})
        assert first == second


class TestContentManifest:
    def test_manifest_lists_payloads_in_canonical_order(self) -> None:
        manifest = build_content_manifest({"b.txt": b"b", "a.txt": b"a"})
        assert [entry.path for entry in manifest.entries] == ["a.txt", "b.txt"]

    def test_manifest_records_digest_and_size(self) -> None:
        manifest = build_content_manifest({"a.txt": b"hello"})
        entry = manifest.entries[0]
        assert entry.sha256 == sha256_hex(b"hello")
        assert entry.size == 5

    def test_integrity_members_cannot_enter_their_own_manifest(self) -> None:
        """The non-recursion rule, asserted directly for both members."""
        for path in (INTEGRITY_FILES_PATH, INTEGRITY_SIGNATURE_PATH):
            with pytest.raises(MalformedIntegrityManifest):
                build_content_manifest({path: b"x"})

    def test_parsed_manifest_rejects_self_listing(self) -> None:
        document: dict[str, object] = {
            "files_format_version": FILES_FORMAT_VERSION,
            "files": [{"path": INTEGRITY_FILES_PATH, "sha256": "a" * 64, "size": 1}],
        }
        with pytest.raises(MalformedIntegrityManifest):
            parse_content_manifest(canonical_json_bytes(document))

    def test_parse_round_trips_canonical_bytes(self) -> None:
        manifest = build_content_manifest({"a.txt": b"a", "b.txt": b"b"})
        assert parse_content_manifest(manifest.canonical_bytes()) == manifest

    def test_parse_rejects_non_canonical_bytes(self) -> None:
        """A manifest that is not byte-identical to its own canonical form is rejected.

        The digest is defined over canonical bytes, so accepting a non-canonical document would
        make the signed value ambiguous.
        """
        manifest = build_content_manifest({"a.txt": b"a"})
        pretty = json.dumps(
            json.loads(manifest.canonical_bytes().decode()), indent=2, sort_keys=True
        ).encode()
        with pytest.raises(MalformedIntegrityManifest):
            parse_content_manifest(pretty)

    def test_parse_rejects_unsupported_format_version(self) -> None:
        document: dict[str, object] = {"files_format_version": "2", "files": []}
        with pytest.raises(MalformedIntegrityManifest):
            parse_content_manifest(canonical_json_bytes(document))

    def test_parse_rejects_malformed_digest(self) -> None:
        document: dict[str, object] = {
            "files_format_version": FILES_FORMAT_VERSION,
            "files": [{"path": "a.txt", "sha256": "NOTHEX", "size": 1}],
        }
        with pytest.raises(MalformedIntegrityManifest):
            parse_content_manifest(canonical_json_bytes(document))

    def test_parse_rejects_negative_size(self) -> None:
        document: dict[str, object] = {
            "files_format_version": FILES_FORMAT_VERSION,
            "files": [{"path": "a.txt", "sha256": "a" * 64, "size": -1}],
        }
        with pytest.raises(MalformedIntegrityManifest):
            parse_content_manifest(canonical_json_bytes(document))

    def test_parse_rejects_non_object_root(self) -> None:
        with pytest.raises(MalformedIntegrityManifest):
            parse_content_manifest(b"[1,2,3]")

    def test_parse_rejects_invalid_json(self) -> None:
        with pytest.raises(MalformedIntegrityManifest):
            parse_content_manifest(b"{not json")


class TestContentDigest:
    def test_digest_is_sha256_of_canonical_manifest_bytes(self) -> None:
        manifest = build_content_manifest({"a.txt": b"a"})
        assert content_digest(manifest) == sha256_hex(manifest.canonical_bytes())

    def test_digest_is_sensitive_to_one_byte(self) -> None:
        first = content_digest(build_content_manifest({"a.txt": b"a"}))
        second = content_digest(build_content_manifest({"a.txt": b"b"}))
        assert first != second

    def test_digest_is_stable_for_equal_content(self) -> None:
        first = content_digest(build_content_manifest({"a.txt": b"a", "b.txt": b"b"}))
        second = content_digest(build_content_manifest({"b.txt": b"b", "a.txt": b"a"}))
        assert first == second

    def test_digest_is_not_the_archive_hash(self) -> None:
        """The signed identity is the payload manifest, never the container bytes."""
        manifest = build_content_manifest({"a.txt": b"a"})
        digest = content_digest(manifest)
        assert digest != sha256_hex(manifest.canonical_bytes() + b"extra archive framing")


class TestPayloadVerification:
    def test_matching_payloads_verify(self) -> None:
        payloads = {"a.txt": b"a", "b.txt": b"b"}
        verify_payload_against_manifest(build_content_manifest(payloads), payloads)

    def test_changed_payload_fails(self) -> None:
        manifest = build_content_manifest({"a.txt": b"a"})
        with pytest.raises(PackageIntegrityMismatch):
            verify_payload_against_manifest(manifest, {"a.txt": b"b"})

    def test_missing_payload_fails(self) -> None:
        manifest = build_content_manifest({"a.txt": b"a", "b.txt": b"b"})
        with pytest.raises(PackageIntegrityMismatch):
            verify_payload_against_manifest(manifest, {"a.txt": b"a"})

    def test_extra_payload_fails(self) -> None:
        manifest = build_content_manifest({"a.txt": b"a"})
        with pytest.raises(PackageIntegrityMismatch):
            verify_payload_against_manifest(manifest, {"a.txt": b"a", "b.txt": b"b"})

    def test_size_mismatch_fails_even_with_matching_prefix(self) -> None:
        manifest = build_content_manifest({"a.txt": b"a"})
        with pytest.raises(PackageIntegrityMismatch):
            verify_payload_against_manifest(manifest, {"a.txt": b"ab"})


class TestSignedEnvelope:
    def test_envelope_contains_exactly_the_frozen_fields(self) -> None:
        envelope = build_signed_envelope(
            package_id="com.acme.invoice",
            package_version="1.2.3",
            manifest_version="1",
            digest="a" * 64,
        )
        assert set(json.loads(envelope.decode())) == {
            "signature_format_version",
            "package_id",
            "package_version",
            "manifest_version",
            "content_digest",
        }

    def test_signature_format_version_is_the_string_one(self) -> None:
        envelope = build_signed_envelope(
            package_id="com.acme.invoice",
            package_version="1.2.3",
            manifest_version="1",
            digest="a" * 64,
        )
        assert json.loads(envelope.decode())["signature_format_version"] == "1"
        assert b'"signature_format_version":"1"' in envelope

    def test_envelope_keys_are_sorted(self) -> None:
        envelope = build_signed_envelope(
            package_id="com.acme.invoice",
            package_version="1.2.3",
            manifest_version="1",
            digest="a" * 64,
        )
        assert envelope == canonical_json_bytes(json.loads(envelope.decode()))

    def test_envelope_is_deterministic(self) -> None:
        args = {
            "package_id": "com.acme.invoice",
            "package_version": "1.2.3",
            "manifest_version": "1",
            "digest": "a" * 64,
        }
        assert build_signed_envelope(**args) == build_signed_envelope(**args)


class TestFingerprint:
    def test_fingerprint_is_sha256_of_raw_key(self) -> None:
        key = bytes(range(32))
        assert signature_fingerprint(key) == sha256_hex(key)

    def test_fingerprint_is_hex_and_64_chars(self) -> None:
        fingerprint = signature_fingerprint(bytes(range(32)))
        assert len(fingerprint) == 64
        assert all(character in "0123456789abcdef" for character in fingerprint)

    def test_different_keys_give_different_fingerprints(self) -> None:
        assert signature_fingerprint(bytes(range(32))) != signature_fingerprint(
            bytes(range(32, 64))
        )


class TestPackagingNormalization:
    """`packaging` normalizes *Python* identity only; NervOS package identity is untouched."""

    def test_distribution_names_are_pep503_normalized(self) -> None:
        assert normalized_distribution_name("Acme_Invoice.Agent") == "acme-invoice-agent"
        assert normalized_distribution_name("acme-invoice-agent") == "acme-invoice-agent"

    def test_versions_are_pep440_normalized(self) -> None:
        assert normalized_distribution_version("2.0.0") == "2.0.0"
        assert normalized_distribution_version("2.0") == "2.0"

    def test_invalid_pep440_version_is_rejected(self) -> None:
        with pytest.raises(MalformedIntegrityManifest):
            normalized_distribution_version("1.2.3-four.five.six")
