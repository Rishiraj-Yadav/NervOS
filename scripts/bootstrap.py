"""Install the project-local Python and JavaScript dependencies."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MINIMUM_PYTHON = (3, 12)
MINIMUM_NODE = (22, 12)
MAXIMUM_NODE_MAJOR = 25
PNPM_MAJOR = 10
# Keep in step with the uv version pinned in .github/workflows/ci.yml.
MINIMUM_UV = (0, 9, 15)


def require_command(name: str) -> str:
    """Return a command path or exit with an actionable message."""
    path = shutil.which(name)
    if path is None:
        raise SystemExit(
            f"Required command '{name}' was not found. Install it outside this script, then retry."
        )
    return path


def read_version(command: str, *arguments: str) -> tuple[int, ...]:
    """Read a dotted numeric version from a command."""
    result = subprocess.run(
        [command, *arguments],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    match = re.search(r"(\d+(?:\.\d+)+)", result.stdout)
    if match is None:
        raise SystemExit(f"Could not determine the version reported by {command!r}.")
    return tuple(int(part) for part in match.group(1).split("."))


def run(command: Sequence[str]) -> None:
    """Run one bootstrap command from the repository root."""
    print(f"+ {' '.join(command)}", flush=True)
    subprocess.run(list(command), cwd=ROOT, check=True)


def validate_prerequisites() -> tuple[str, str]:
    """Validate local runtimes without installing system packages."""
    if sys.version_info < MINIMUM_PYTHON:
        raise SystemExit("NervOS requires Python 3.12 or newer.")

    uv = require_command("uv")
    node = require_command("node")
    pnpm = require_command("pnpm")

    uv_version = read_version(uv, "--version")
    if uv_version < MINIMUM_UV:
        minimum = ".".join(str(part) for part in MINIMUM_UV)
        raise SystemExit(f"NervOS requires uv {minimum} or newer.")

    node_version = read_version(node, "--version")
    if node_version < MINIMUM_NODE or node_version[0] >= MAXIMUM_NODE_MAJOR:
        raise SystemExit("NervOS requires Node.js >=22.12 and <25 for Stage A.")

    pnpm_version = read_version(pnpm, "--version")
    if pnpm_version[0] != PNPM_MAJOR:
        raise SystemExit("NervOS requires pnpm 10.x for Stage A.")

    return uv, pnpm


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse bootstrap options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-browser",
        action="store_true",
        help="Install dependencies only; skip Playwright Chromium provisioning.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Install locked dependencies into project-local environments."""
    args = parse_args(argv)
    uv, pnpm = validate_prerequisites()
    run([uv, "sync", "--frozen", "--all-packages"])
    run([pnpm, "install", "--frozen-lockfile"])
    if args.skip_browser:
        print("NervOS Stage A dependencies are ready (Playwright Chromium skipped).")
        return 0
    run([pnpm, "--dir", "apps/web", "exec", "playwright", "install", "chromium"])
    print("NervOS Stage A dependencies and Playwright Chromium are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
