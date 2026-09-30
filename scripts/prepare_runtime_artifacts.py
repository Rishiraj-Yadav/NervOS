"""Build the two internal package-runtime wheels into the controlled local package store.

This is a bootstrap/preparation operation. Package installation only consumes the wheels that this
script has already prepared, so an untrusted package install cannot trigger a build or a download.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = Path.home() / ".nervos" / "packages" / "runtime"
PACKAGES = ("nervos-sdk", "nervos-package-host")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    destination = args.destination.expanduser().resolve(strict=False)
    destination.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required to prepare NervOS runtime artifacts")
    for package in PACKAGES:
        subprocess.run(
            [
                uv,
                "build",
                "--offline",
                "--wheel",
                "--out-dir",
                str(destination),
                str(ROOT / "packages" / package),
            ],
            cwd=ROOT,
            check=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
