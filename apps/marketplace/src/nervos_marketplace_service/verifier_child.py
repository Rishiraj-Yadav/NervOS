"""Credential-free static verification executable; bounded JSON evidence only."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import time
from pathlib import Path

from nervos_marketplace_service.infrastructure.process_limits import apply_limits, usage_snapshot


def verify(path: Path) -> dict[str, object]:
    # Imports occur after resource containment; no agent modules are imported.
    from nervos_core.application.package_archive import (
        ArchiveValidationProfile,
        BoundedArchiveReader,
    )
    from nervos_core.application.package_verification import verify_package

    with path.open("rb") as source:
        verified = verify_package(source=source)
        reader = BoundedArchiveReader(source, profile=ArchiveValidationProfile.NERVOS_V1)
        manifest = reader.read("manifest.yaml")
        entry = next(item for item in verified.entries if item.path == "manifest.yaml")
        if len(manifest) != entry.size or hashlib.sha256(manifest).hexdigest() != entry.sha256:
            raise ValueError("Verified manifest evidence mismatch")
    result = verified.manifest
    return {
        "package_id": result.package_id,
        "exact_version": result.package_version,
        "archive_sha256": verified.archive_digest,
        "content_digest": verified.content_digest,
        "signer_fingerprint": verified.signer_fingerprint,
        "public_key": base64.b64encode(verified.signer_public_key).decode(),
        "manifest_bytes": base64.b64encode(manifest).decode(),
        "manifest_version": 1,
        "runtime_language": result.runtime.language,
        "runtime_python": result.runtime.python,
        "nervos_min_version": result.nervos.min_version.value,
        "nervos_max_version": result.nervos.max_version.value,
        "size_bytes": path.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--memory-bytes", type=int, required=True)
    parser.add_argument("--cpu-seconds", type=int, default=60)
    args = parser.parse_args()
    started = time.monotonic()
    try:
        apply_limits(args.memory_bytes, args.cpu_seconds)
        result = verify(args.archive)
        resources = usage_snapshot()
        print(
            json.dumps(
                {
                    "evidence": result,
                    **resources,
                    "wall_seconds": time.monotonic() - started,
                },
                separators=(",", ":"),
            )
        )
        return 0
    except MemoryError:
        print('{"error":"verification_resource_exceeded"}')
        return 2
    except ValueError:
        print('{"error":"package_invalid"}')
        return 3
    except Exception:
        print('{"error":"verification_failed"}')
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
