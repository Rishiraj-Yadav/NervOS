"""G2 Ed25519 signing and verification.

Two things are exercised together here and must not be conflated: the *mechanics* (a signature
verifies with a key) and the *non-claim* (that key is not thereby trusted). Several tests assert the
second explicitly so a future reader cannot mistake verification for trust.
"""

from __future__ import annotations

import base64
import json

import pytest
from nervos_core.application.package_integrity import (
    InvalidPackageSignature,
    SignatureEnvelope,
    UnsupportedSignatureFormat,
    build_signed_envelope,
    canonical_json_bytes,
    signature_fingerprint,
)
from nervos_core.application.package_signing import (
    Ed25519PackageSigner,
    envelope_for_verified_state,
    parse_signature_document,
    signature_bytes,
    signature_document,
    verify_package_signature,
)
from package_fixtures import OTHER_TEST_SIGNING_SEED, TEST_SIGNING_SEED


def _envelope() -> bytes:
    return build_signed_envelope(
        package_id="com.acme.invoice",
        package_version="1.2.3",
        manifest_version="1",
        digest="a" * 64,
    )


def _signer(seed: bytes = TEST_SIGNING_SEED) -> Ed25519PackageSigner:
    return Ed25519PackageSigner.from_private_bytes(seed)


class TestSigner:
    def test_public_key_is_32_raw_bytes(self) -> None:
        assert len(_signer().public_key_bytes()) == 32

    def test_signature_is_64_bytes(self) -> None:
        assert len(_signer().sign(_envelope())) == 64

    def test_signing_is_deterministic(self) -> None:
        """Ed25519 is deterministic for a fixed key and message, which is what makes byte-identical
        rebuilds possible at all."""
        assert _signer().sign(_envelope()) == _signer().sign(_envelope())

    def test_different_keys_sign_differently(self) -> None:
        assert _signer().sign(_envelope()) != _signer(OTHER_TEST_SIGNING_SEED).sign(_envelope())

    def test_different_messages_sign_differently(self) -> None:
        other = _envelope_for("b" * 64)
        assert _signer().sign(_envelope()) != _signer().sign(other)


def _envelope_for(digest: str) -> bytes:
    return build_signed_envelope(
        package_id="com.acme.invoice",
        package_version="1.2.3",
        manifest_version="1",
        digest=digest,
    )


class TestSignatureDocument:
    def test_document_shape(self) -> None:
        document, fingerprint = signature_document(_envelope(), _signer())
        assert set(document) == {
            "algorithm",
            "key_fingerprint",
            "public_key",
            "signature",
            "signature_format_version",
        }
        assert document["algorithm"] == "ed25519"
        assert document["signature_format_version"] == "1"
        assert document["key_fingerprint"] == fingerprint

    def test_fingerprint_matches_sha256_of_embedded_key(self) -> None:
        document, fingerprint = signature_document(_envelope(), _signer())
        public_key = base64.b64decode(str(document["public_key"]))
        assert signature_fingerprint(public_key) == fingerprint

    def test_encodings_are_base64(self) -> None:
        document, _ = signature_document(_envelope(), _signer())
        assert len(base64.b64decode(str(document["public_key"]), validate=True)) == 32
        assert len(base64.b64decode(str(document["signature"]), validate=True)) == 64

    def test_document_bytes_are_canonical(self) -> None:
        document, _ = signature_document(_envelope(), _signer())
        assert signature_bytes(document) == canonical_json_bytes(document)


class TestVerification:
    def _signed(self, seed: bytes = TEST_SIGNING_SEED):
        envelope = _envelope()
        document, fingerprint = signature_document(envelope, _signer(seed))
        parsed = parse_signature_document(signature_bytes(document))
        return envelope, parsed, fingerprint

    def test_valid_signature_verifies(self) -> None:
        envelope, parsed, fingerprint = self._signed()
        assert verify_package_signature(parsed, expected_envelope=envelope) == fingerprint

    def test_tampered_envelope_fails(self) -> None:
        _, parsed, _ = self._signed()
        with pytest.raises(InvalidPackageSignature):
            verify_package_signature(parsed, expected_envelope=_envelope_for("b" * 64))

    def test_wrong_key_fails(self) -> None:
        """A signature made by another key must not verify against this document's key."""
        envelope, _, _ = self._signed()
        other_document, _ = signature_document(envelope, _signer(OTHER_TEST_SIGNING_SEED))
        forged = dict(other_document)
        forged["signature"] = base64.b64encode(b"\x00" * 64).decode()
        parsed = parse_signature_document(signature_bytes(forged))
        with pytest.raises(InvalidPackageSignature):
            verify_package_signature(parsed, expected_envelope=envelope)

    def test_modified_signature_bytes_fail(self) -> None:
        envelope, _, _ = self._signed()
        document, _ = signature_document(envelope, _signer())
        raw_signature = bytearray(base64.b64decode(str(document["signature"])))
        raw_signature[0] ^= 0xFF
        document["signature"] = base64.b64encode(bytes(raw_signature)).decode()
        parsed = parse_signature_document(signature_bytes(document))
        with pytest.raises(InvalidPackageSignature):
            verify_package_signature(parsed, expected_envelope=envelope)

    def test_fingerprint_mismatch_fails(self) -> None:
        envelope, parsed, _ = self._signed()
        forged = SignatureEnvelope(
            algorithm=parsed.algorithm,
            signature_format_version=parsed.signature_format_version,
            public_key=parsed.public_key,
            key_fingerprint="0" * 64,
            signature=parsed.signature,
        )
        with pytest.raises(InvalidPackageSignature):
            verify_package_signature(forged, expected_envelope=envelope)

    def test_signature_validity_establishes_no_trust(self) -> None:
        """Verification returns a fingerprint, not a verdict about the publisher.

        There is no trust store, allowlist, or revocation list anywhere in G2, so an unknown signer
        verifies exactly like a known one. This asserts the shape of that non-claim.
        """
        envelope, parsed, fingerprint = self._signed()
        result = verify_package_signature(parsed, expected_envelope=envelope)
        assert result == fingerprint
        assert not hasattr(parsed, "trusted")
        assert not hasattr(parsed, "publisher")


class TestSignatureDocumentParsing:
    def _document(self, **overrides: object) -> bytes:
        document, _ = signature_document(_envelope(), _signer())
        document.update(overrides)
        return signature_bytes(document)

    def test_valid_document_parses(self) -> None:
        parsed = parse_signature_document(self._document())
        assert parsed.algorithm == "ed25519"
        assert parsed.signature_format_version == "1"

    def test_unsupported_algorithm_is_rejected(self) -> None:
        with pytest.raises(UnsupportedSignatureFormat):
            parse_signature_document(self._document(algorithm="rsa"))

    def test_unsupported_format_version_is_rejected(self) -> None:
        with pytest.raises(UnsupportedSignatureFormat):
            parse_signature_document(self._document(signature_format_version="2"))

    def test_integer_format_version_is_rejected(self) -> None:
        """The frozen value is the string "1"; a JSON number must not be accepted as equivalent."""
        with pytest.raises(UnsupportedSignatureFormat):
            parse_signature_document(self._document(signature_format_version=1))

    def test_malformed_base64_is_rejected(self) -> None:
        with pytest.raises(InvalidPackageSignature):
            parse_signature_document(self._document(signature="not base64!!"))

    def test_wrong_key_length_is_rejected(self) -> None:
        with pytest.raises(InvalidPackageSignature):
            parse_signature_document(self._document(public_key=base64.b64encode(b"short").decode()))

    def test_wrong_signature_length_is_rejected(self) -> None:
        with pytest.raises(InvalidPackageSignature):
            parse_signature_document(
                self._document(signature=base64.b64encode(b"\x00" * 32).decode())
            )

    def test_non_object_root_is_rejected(self) -> None:
        with pytest.raises(InvalidPackageSignature):
            parse_signature_document(b"[1,2,3]")

    def test_invalid_json_is_rejected(self) -> None:
        with pytest.raises(InvalidPackageSignature):
            parse_signature_document(b"{broken")

    def test_missing_fingerprint_is_rejected(self) -> None:
        document, _ = signature_document(_envelope(), _signer())
        document.pop("key_fingerprint")
        with pytest.raises(InvalidPackageSignature):
            parse_signature_document(json.dumps(document).encode())


class TestEnvelopeReconstruction:
    def test_rebuilt_envelope_matches_signed_envelope(self) -> None:
        """The verifier rebuilds the envelope from verified state rather than reading it back."""
        envelope = _envelope()
        rebuilt = envelope_for_verified_state(
            package_id="com.acme.invoice",
            package_version="1.2.3",
            manifest_version="1",
            digest="a" * 64,
        )
        assert rebuilt == envelope
