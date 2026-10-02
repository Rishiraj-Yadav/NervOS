"""Internal storage evidence, independent of package identity and public DTOs."""

import re
from dataclasses import dataclass

from nervos_core.domain.package_installation import validate_sha256


def validate_version_id(value: str) -> str:
    if value == "null" or re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,1024}", value) is None:
        raise ValueError("Invalid storage version evidence")
    return value


@dataclass(frozen=True)
class FinalizedArtifactRef:
    archive_sha256: str
    size_bytes: int
    version_id: str

    def __post_init__(self) -> None:
        validate_sha256(self.archive_sha256)
        validate_version_id(self.version_id)
        if not 0 < self.size_bytes <= 256 * 1024**2:
            raise ValueError("Invalid artifact length")

    @property
    def key(self) -> str:
        digest = self.archive_sha256
        return f"artifacts/sha256/{digest[:2]}/{digest}.nervos"
