"""Run the repository check groups owned by the Stage A tooling."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHECKS: dict[str, tuple[tuple[str, ...], ...]] = {
    "lint": (
        ("uv", "run", "ruff", "check", "."),
        ("uv", "run", "ruff", "format", "--check", "."),
        ("pnpm", "lint"),
    ),
    "typecheck": (
        ("uv", "run", "pyright"),
        ("pnpm", "typecheck"),
    ),
    "test": (
        ("uv", "run", "pytest"),
        ("pnpm", "test"),
    ),
    "security": (("uv", "run", "python", "scripts/security_scan.py"),),
    "e2e": (("uv", "run", "python", "scripts/e2e.py"),),
}
# `check` is everything fast, offline and deterministic: no browser and no
# process-spawning service orchestration. The deterministic Playwright journey
# stays in its own `e2e` group so routine work never needs Chromium.
CHECKS["check"] = CHECKS["lint"] + CHECKS["typecheck"] + CHECKS["test"] + CHECKS["security"]


def resolve_command(command: Sequence[str]) -> list[str]:
    """Resolve the executable without invoking a platform shell."""
    executable = shutil.which(command[0])
    if executable is None:
        raise SystemExit(
            f"Required command '{command[0]}' was not found. Run the bootstrap prerequisites first."
        )
    return [executable, *command[1:]]


def run_check(group: str) -> None:
    """Run every command in a named check group."""
    for command in CHECKS[group]:
        resolved = resolve_command(command)
        print(f"+ {' '.join(command)}", flush=True)
        subprocess.run(resolved, cwd=ROOT, check=True)


def parse_args() -> argparse.Namespace:
    """Parse the requested check group."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=tuple(CHECKS))
    return parser.parse_args()


def main() -> int:
    """Run the selected repository checks."""
    args = parse_args()
    run_check(args.group)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
