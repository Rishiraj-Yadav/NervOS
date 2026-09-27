"""Canonical JSON, the `integrity/files.json` manifest, the content digest, and the signed envelope.

The integrity chain is frozen (Stage-G §23, ADR 0026) and deliberately non-recursive:

```text
payload files -> files.json -> SHA-256(files.json canonical bytes) -> content_digest
```

`files.json` and `signature.json` never list or hash themselves. If they did, the digest could not
be computed at all: `files.json` would have to contain its own hash. Every function here keeps that
rule visible rather than incidental.

The signed envelope is *not* stored in the archive. The verifier recomputes it from verified
payload state, so there is no second copy of the signed fields that could disagree with what was
actually verified.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from nervos_core.application.package_paths import canonical_order

# The two members excluded from the payload manifest. This is the non-recursion rule, named once.
INTEGRITY_FILES_PATH = "integrity/files.json"
INTEGRITY_SIGNATURE_PATH = "integrity/signature.json"
NON_PAYLOAD_PATHS = frozenset({INTEGRITY_FILES_PATH, INTEGRITY_SIGNATURE_PATH})

# Amendment: `signature_format_version` is the string "1", not an integer. It is a version token,
# and keeping it a string avoids a JSON number that some encoder could render as `1.0`.
SIGNATURE_FORMAT_VERSION = "1"
FILES_FORMAT_VERSION = "1"
_ALGORITHM = "ed25519"


class MalformedIntegrityManifest(ValueError):
    """Raised when `integrity/files.json` is not a valid canonical payload manifest."""


class PackageIntegrityMismatch(ValueError):
    """Raised when packaged bytes do not match the declared integrity manifest."""


class UnsupportedSignatureFormat(ValueError):
    """Raised when a signature document uses an algorithm or format version V1 does not define."""


class InvalidPackageSignature(ValueError):
    """Raised when a signature document is malformed, or its signature does not verify."""


class SignerFingerprintMismatch(ValueError):
    """Raised when the embedded public key does not match its declared fingerprint."""


def canonical_json_bytes(value: object) -> bytes:
    """Encode one JSON-compatible value in the repository's canonical form.

    UTF-8, sorted keys, compact separators, no `NaN`/`Infinity`. This matches the idiom already
    used by `domain/context.py`, `domain/tools.py`, and `application/package_config_schema.py`, so
    the repository has one canonical JSON style rather than several.
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(payload: bytes) -> str:
    """SHA-256 over exact bytes, lowercase hex."""
    return hashlib.sha256(payload).hexdigest()


def normalized_distribution_name(value: str) -> str:
    """PEP 503 normalization for a Python distribution name, via `packaging`.

    Used only for comparing the lock file's names against wheel `METADATA`. It is deliberately
    unrelated to NervOS package identity, which keeps its own frozen grammar and is never
    PEP-normalized.
    """
    return canonicalize_name(value)


def normalized_distribution_version(value: str) -> str:
    """PEP 440 normalization for a dependency version, via `packaging`.

    PEP 440 is a Python-dependency concern only (ADR 0024): it never applies to a NervOS package
    version, which is strict SemVer.
    """
    try:
        return str(Version(value))
    except InvalidVersion as error:
        raise MalformedIntegrityManifest("dependency version is not valid PEP 440") from error


@dataclass(frozen=True, slots=True)
class PayloadEntry:
    """One payload member's frozen identity: canonical path, digest, and exact byte length."""

    path: str
    sha256: str
    size: int

    def as_json(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True, slots=True)
class ContentManifest:
    """The validated `integrity/files.json` payload manifest."""

    entries: tuple[PayloadEntry, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "files_format_version": FILES_FORMAT_VERSION,
            "files": [entry.as_json() for entry in self.entries],
        }

    def canonical_bytes(self) -> bytes:
        """The exact bytes `content_digest` is computed over."""
        return canonical_json_bytes(self.as_json())


# The payload entry type is the verified-fact shape that leaves G2, so it is also re-exported under
# the name the verifier's result uses.
AcceptedFileEntry = PayloadEntry


def build_content_manifest(payloads: Mapping[str, bytes]) -> ContentManifest:
    """Build the payload manifest from canonical path -> exact payload bytes.

    Every supplied path is a payload member; the integrity members are excluded here rather than by
    the caller, so no call site can accidentally include them.
    """
    entries: list[PayloadEntry] = []
    for path in canonical_order(payloads):
        if path in NON_PAYLOAD_PATHS:
            raise MalformedIntegrityManifest(
                "integrity files must not be listed in their own payload manifest"
            )
        payload = payloads[path]
        entries.append(PayloadEntry(path=path, sha256=sha256_hex(payload), size=len(payload)))
    return ContentManifest(entries=tuple(entries))


def parse_content_manifest(raw: bytes) -> ContentManifest:
    """Parse and validate `integrity/files.json`, rejecting anything non-canonical."""
    try:
        parsed_document: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MalformedIntegrityManifest("integrity/files.json must be valid UTF-8 JSON") from error
    if not isinstance(parsed_document, dict):
        raise MalformedIntegrityManifest("integrity/files.json root must be an object")
    document = cast("dict[str, object]", parsed_document)
    if document.get("files_format_version") != FILES_FORMAT_VERSION:
        raise MalformedIntegrityManifest("unsupported files_format_version")
    declared_entries = document.get("files")
    if not isinstance(declared_entries, list):
        raise MalformedIntegrityManifest("integrity/files.json must list files")

    parsed: list[PayloadEntry] = []
    for raw_entry in cast("list[object]", declared_entries):
        if not isinstance(raw_entry, dict):
            raise MalformedIntegrityManifest("integrity entries must be objects")
        entry = cast("dict[str, object]", raw_entry)
        path = entry.get("path")
        digest = entry.get("sha256")
        size = entry.get("size")
        if not isinstance(path, str) or not isinstance(digest, str) or not isinstance(size, int):
            raise MalformedIntegrityManifest("integrity entries must carry path, sha256, and size")
        if isinstance(size, bool) or size < 0:
            raise MalformedIntegrityManifest("integrity entry sizes must be non-negative integers")
        if path in NON_PAYLOAD_PATHS:
            raise MalformedIntegrityManifest(
                "integrity files must not be listed in their own payload manifest"
            )
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise MalformedIntegrityManifest(
                "integrity entry digests must be lowercase SHA-256 hex"
            )
        parsed.append(PayloadEntry(path=path, sha256=digest, size=size))

    manifest = ContentManifest(entries=tuple(parsed))
    # A manifest that is not in canonical order, or that is not byte-identical to its own
    # re-canonicalization, is not the canonical document the digest is defined over.
    if manifest.canonical_bytes() != raw:
        raise MalformedIntegrityManifest("integrity/files.json is not canonical")
    return manifest


def content_digest(manifest: ContentManifest) -> str:
    """`content_digest = SHA-256(canonical files.json bytes)` (frozen formula)."""
    return sha256_hex(manifest.canonical_bytes())


def verify_payload_against_manifest(
    manifest: ContentManifest, payloads: Mapping[str, bytes]
) -> None:
    """Recompute every payload hash and require an exact, complete match."""
    declared = {entry.path: entry for entry in manifest.entries}
    if set(declared) != set(payloads):
        missing = sorted(set(declared) - set(payloads))
        extra = sorted(set(payloads) - set(declared))
        raise PackageIntegrityMismatch(
            f"payload manifest does not describe the archive (missing={missing}, extra={extra})"
        )
    for path, payload in payloads.items():
        entry = declared[path]
        if len(payload) != entry.size:
            raise PackageIntegrityMismatch(f"payload {path!r} length does not match the manifest")
        if sha256_hex(payload) != entry.sha256:
            raise PackageIntegrityMismatch(f"payload {path!r} digest does not match the manifest")


def build_signed_envelope(
    *,
    package_id: str,
    package_version: str,
    manifest_version: str,
    digest: str,
) -> bytes:
    """The canonical signed envelope bytes.

    Exactly the five frozen fields (Stage-G §24). `signature_format_version` is the string "1".
    Rebuilt from verified state at verification time rather than read back from the archive.
    """
    return canonical_json_bytes(
        {
            "content_digest": digest,
            "manifest_version": manifest_version,
            "package_id": package_id,
            "package_version": package_version,
            "signature_format_version": SIGNATURE_FORMAT_VERSION,
        }
    )


@dataclass(frozen=True, slots=True)
class SignatureEnvelope:
    """A parsed, structurally valid `integrity/signature.json`."""

    algorithm: str
    signature_format_version: str
    public_key: bytes
    key_fingerprint: str
    signature: bytes


def signature_fingerprint(public_key: bytes) -> str:
    """`fingerprint = SHA-256(raw Ed25519 public key bytes)`, lowercase hex.

    This identifies a key. It does not vouch for it: the fingerprint says which key signed, never
    that the key is trusted (Stage-G §24; Stage H/I own publisher trust).
    """
    return sha256_hex(public_key)


def require_supported_algorithm(algorithm: object) -> str:
    """Accept exactly one V1 algorithm. There is no algorithm negotiation in V1."""
    if algorithm != _ALGORITHM:
        raise UnsupportedSignatureFormat(f"unsupported signature algorithm: {algorithm!r}")
    return _ALGORITHM
