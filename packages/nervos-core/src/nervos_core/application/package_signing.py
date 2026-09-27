"""Ed25519 signing and verification mechanics for `.nervos` packages.

The one thing to hold onto while reading this module: **a valid signature is mechanism, not trust**
(Stage-G §24, ADR 0026). Verifying proves "this signature verifies with this key". It does not prove
"this key is trusted", and Stage G deliberately has no trust store, allowlist, or revocation list to
make it mean that. Publisher trust is a Stage H/I concern; the operator is shown the fingerprint and
decides at install time.

The signing key enters through a small injected protocol. G2 does not build a secret manager, never
persists a private key, and never logs key material; a caller (a test, or future developer tooling)
supplies whatever signer it has.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from nervos_core.application.package_integrity import (
    SIGNATURE_FORMAT_VERSION,
    InvalidPackageSignature,
    SignatureEnvelope,
    UnsupportedSignatureFormat,
    build_signed_envelope,
    canonical_json_bytes,
    require_supported_algorithm,
    signature_fingerprint,
)

_RAW_PUBLIC_KEY_BYTES = 32
_RAW_SIGNATURE_BYTES = 64


class PackageSigner(Protocol):
    """Signs a canonical envelope and reports the public key that verifies it.

    Kept deliberately narrow: the protocol exposes no key-management surface, so nothing here can
    grow into the Stage-H secret manager, and a test can satisfy it with a deterministic in-memory
    key.
    """

    def sign(self, envelope: bytes) -> bytes: ...

    def public_key_bytes(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class Ed25519PackageSigner:
    """A `PackageSigner` backed by an Ed25519 private key supplied by the caller.

    Ed25519 signing is deterministic: the same key over the same message always produces the same
    signature, which is what lets two identical builds produce byte-identical archives.
    """

    private_key: Ed25519PrivateKey

    @classmethod
    def from_private_bytes(cls, raw: bytes) -> Ed25519PackageSigner:
        return cls(private_key=Ed25519PrivateKey.from_private_bytes(raw))

    def sign(self, envelope: bytes) -> bytes:
        return self.private_key.sign(envelope)

    def public_key_bytes(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )


def signature_document(
    envelope: bytes,
    signer: PackageSigner,
) -> tuple[dict[str, object], str]:
    """Produce the `integrity/signature.json` document plus the signer fingerprint.

    The envelope is not stored: the verifier recomputes it from verified payload state, so storing
    it would only create a second copy of the signed fields that could disagree with reality.
    """
    public_key = signer.public_key_bytes()
    if len(public_key) != _RAW_PUBLIC_KEY_BYTES:
        raise InvalidPackageSignature("Ed25519 public keys are exactly 32 bytes")
    signature = signer.sign(envelope)
    if len(signature) != _RAW_SIGNATURE_BYTES:
        raise InvalidPackageSignature("Ed25519 signatures are exactly 64 bytes")

    fingerprint = signature_fingerprint(public_key)
    document: dict[str, object] = {
        "algorithm": "ed25519",
        "key_fingerprint": fingerprint,
        "public_key": base64.b64encode(public_key).decode("ascii"),
        "signature": base64.b64encode(signature).decode("ascii"),
        "signature_format_version": SIGNATURE_FORMAT_VERSION,
    }
    return document, fingerprint


def signature_bytes(document: Mapping[str, object]) -> bytes:
    """The canonical bytes of a `signature.json` document, for embedding in the archive."""
    return canonical_json_bytes(document)


def parse_signature_document(raw: bytes) -> SignatureEnvelope:
    """Parse and structurally validate `integrity/signature.json`.

    Rejects unknown algorithms, wrong format versions, malformed base64, and wrong key or signature
    lengths before any cryptographic work happens.
    """
    try:
        parsed_document: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidPackageSignature(
            "integrity/signature.json must be valid UTF-8 JSON"
        ) from error
    if not isinstance(parsed_document, dict):
        raise InvalidPackageSignature("integrity/signature.json root must be an object")
    document = cast("dict[str, object]", parsed_document)

    version = document.get("signature_format_version")
    if version != SIGNATURE_FORMAT_VERSION or not isinstance(version, str):
        raise UnsupportedSignatureFormat(f"unsupported signature format version: {version!r}")
    algorithm = require_supported_algorithm(document.get("algorithm"))

    public_key = _decode_base64(document.get("public_key"), _RAW_PUBLIC_KEY_BYTES, "public_key")
    signature = _decode_base64(document.get("signature"), _RAW_SIGNATURE_BYTES, "signature")
    fingerprint = document.get("key_fingerprint")
    if not isinstance(fingerprint, str):
        raise InvalidPackageSignature("signature document must carry a key fingerprint")

    return SignatureEnvelope(
        algorithm=algorithm,
        signature_format_version=version,
        public_key=public_key,
        key_fingerprint=fingerprint,
        signature=signature,
    )


def _decode_base64(value: object, expected_length: int, name: str) -> bytes:
    if not isinstance(value, str):
        raise InvalidPackageSignature(f"signature document must carry a base64 {name}")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise InvalidPackageSignature(f"{name} is not valid base64") from error
    if len(decoded) != expected_length:
        raise InvalidPackageSignature(f"{name} must be exactly {expected_length} bytes")
    return decoded


def verify_package_signature(
    envelope: SignatureEnvelope,
    *,
    expected_envelope: bytes,
) -> str:
    """Verify the fingerprint and the Ed25519 signature over a recomputed envelope.

    Returns the verified fingerprint. Success means the signature is valid **for the embedded key**
    -- never that the key is trusted.
    """
    computed = signature_fingerprint(envelope.public_key)
    if computed != envelope.key_fingerprint:
        raise InvalidPackageSignature(
            "embedded public key does not match the declared key fingerprint"
        )

    public_key = Ed25519PublicKey.from_public_bytes(envelope.public_key)
    try:
        public_key.verify(envelope.signature, expected_envelope)
    except InvalidSignature as error:
        raise InvalidPackageSignature("package signature does not verify") from error
    return computed


def envelope_for_verified_state(
    *,
    package_id: str,
    package_version: str,
    manifest_version: str,
    digest: str,
) -> bytes:
    """Rebuild the signed envelope from verified payload state, never from the archive."""
    return build_signed_envelope(
        package_id=package_id,
        package_version=package_version,
        manifest_version=manifest_version,
        digest=digest,
    )
