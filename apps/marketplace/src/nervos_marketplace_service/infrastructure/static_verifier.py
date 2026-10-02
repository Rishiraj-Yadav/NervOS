"""Run only the credential-free trusted static parser in a bounded child."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast

from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.domain.identity import Record


class StaticVerifier:
    def __init__(
        self, memory_bytes: int = 2 * 1024**3, cpu_seconds: int = 60, wall_seconds: int = 120
    ) -> None:
        self.memory, self.cpu, self.wall = memory_bytes, cpu_seconds, wall_seconds

    def verify(self, path: Path) -> Record:
        # Never forward hosted DB/S3/OIDC credentials or ambient Python/proxy settings.
        environment = {
            key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ
        }
        with tempfile.TemporaryDirectory(prefix="verifier-", dir=path.parent) as directory:
            scratch = Path(directory)
            environment.update({"TEMP": directory, "TMP": directory, "TMPDIR": directory})
            output_path = scratch / "evidence.json"
            try:
                with output_path.open("wb") as output:
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            "-B",
                            "-m",
                            "nervos_marketplace_service.verifier_child",
                            str(path.resolve()),
                            "--memory-bytes",
                            str(self.memory),
                            "--cpu-seconds",
                            str(self.cpu),
                        ],
                        env=environment,
                        cwd=scratch,
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.DEVNULL,
                        timeout=self.wall,
                        check=False,
                    )
                if result.returncode or output_path.stat().st_size > 2 * 1024**2:
                    raise MarketplaceError("package_invalid", 422)
                decoded: object = json.loads(output_path.read_bytes())
                if not isinstance(decoded, dict):
                    raise MarketplaceError("package_invalid", 422)
                payload = cast(dict[str, object], decoded)
                if not isinstance(payload.get("evidence"), dict):
                    raise MarketplaceError("package_invalid", 422)
                return cast(Record, payload["evidence"])
            except (subprocess.TimeoutExpired, ValueError, OSError):
                raise MarketplaceError("verification_failed", 422) from None
