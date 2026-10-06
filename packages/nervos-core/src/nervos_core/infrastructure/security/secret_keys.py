"""File-backed master-key resolution for the Stage H1 Secret Manager (ADR 0032).

The master key never lives in the database or the source tree. The operator points
``NERVOS_SECRETS_KEY_FILE`` at a file containing exactly 32 bytes of base64-encoded key
material (the file may carry a trailing newline). Resolution happens once per process and
is fail-closed: a missing, unreadable, malformed, wrong-length, or (on POSIX)
other-accessible file raises ``SecretStoreUnavailable``, which callers must treat as a
refusal of every secret operation — never as a reason to fall back to plaintext or to a
default key.

On Windows the file mode carries no owner/group/other bits, so the permission rule is
enforced on POSIX only and documented as an operator obligation for NTFS ACLs; the
protected-file contract there is the explicit operator-provisioned path.
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

from nervos_core.application.secrets import MasterKeyResolver, SecretStoreUnavailable

KEY_BYTES = 32

# POSIX mode bits that mean someone other than the owner can read the key file. The file
# must be owner-only (0600 or stricter); a world-readable key defeats the encryption it
# protects, so resolution refuses rather than proceeding.
OTHER_ACCESS_BITS = 0o077


class FileMasterKeyResolver(MasterKeyResolver):
    """Resolve one operator-owned key file into ``(key_version, key_material)``."""

    def __init__(self, key_file: Path, key_version: int) -> None:
        self._key_file = key_file.expanduser().resolve(strict=False)
        self._key_version = key_version
        self._cached: tuple[int, bytes] | None = None

    def resolve(self) -> tuple[int, bytes]:
        """Return the resolved key, reading the file at most once per process."""
        if self._cached is None:
            self._cached = (self._key_version, self._load())
        return self._cached

    def resolve_version(self, version: int) -> bytes:
        if version < 1:
            raise SecretStoreUnavailable("the secret key version is invalid")
        if version == self._key_version:
            return self.resolve()[1]
        historical = self._key_file.with_name(f"{self._key_file.name}.v{version}")
        return FileMasterKeyResolver(historical, version).resolve()[1]

    def _load(self) -> bytes:
        try:
            mode = self._key_file.stat().st_mode
            raw = self._key_file.read_bytes()
        except OSError as error:
            raise SecretStoreUnavailable("the secret key file is missing or unreadable") from error
        if os.name != "nt" and mode & OTHER_ACCESS_BITS:
            raise SecretStoreUnavailable(
                "the secret key file is accessible to other users; restrict it to its owner"
            )
        try:
            key = base64.b64decode(raw.strip(), validate=True)
        except (binascii.Error, ValueError) as error:
            raise SecretStoreUnavailable("the secret key file is malformed") from error
        if len(key) != KEY_BYTES:
            raise SecretStoreUnavailable("the secret key file has the wrong length")
        return key


def generate_key_b64() -> str:
    """Return a fresh 32-byte key as base64, for the operator provisioning command."""
    import os

    return base64.b64encode(os.urandom(KEY_BYTES)).decode("ascii")
