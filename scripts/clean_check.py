"""Verify the working tree in an isolated export without touching the checkout.

The export is exactly the file set Git considers part of the repository: tracked
files plus untracked files that are not ignored. That makes it a faithful snapshot
of the current approved working tree even while the work is still uncommitted, and
it proves the repository is self-contained rather than depending on ignored
developer state.

The active checkout is only ever read. The export is initialized as its own Git
repository and staged with ``git add -A`` so Git-based file enumeration works
inside it; no commit is created, so no Git author identity is required. The
temporary directory is always removed.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPORT_PREFIX = "nervos-a6-clean-"
SUMMARY = "Verify the current working tree in an isolated temporary export."


def resolve_command(command: Sequence[str]) -> list[str]:
    """Resolve the executable without invoking a platform shell."""
    executable = shutil.which(command[0])
    if executable is None:
        raise SystemExit(f"Required command '{command[0]}' was not found.")
    return [executable, *command[1:]]


def repository_files(root: Path = ROOT) -> list[str]:
    """List the repository-relative files Git considers part of the repository."""
    result = subprocess.run(
        [
            resolve_command(("git",))[0],
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return sorted(
        entry.decode("utf-8", errors="surrogateescape")
        for entry in result.stdout.split(b"\x00")
        if entry
    )


def export_repository(destination: Path) -> int:
    """Copy the repository file set into destination, preserving relative paths."""
    copied = 0
    for relative in repository_files():
        source = ROOT / relative
        if not source.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    return copied


def initialize_repository(destination: Path) -> None:
    """Stage the export in its own repository without creating a commit."""
    git = resolve_command(("git",))[0]
    subprocess.run([git, "init", "-q"], cwd=destination, check=True)
    subprocess.run([git, "add", "-A"], cwd=destination, check=True)


def verification_steps() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return the repository-owned verification commands, in order."""
    return (
        ("lockfile", ("uv", "lock", "--check")),
        ("bootstrap", (sys.executable, "scripts/bootstrap.py")),
        ("check", ("uv", "run", "python", "scripts/check.py", "check")),
        ("build", ("pnpm", "build")),
        ("e2e", ("uv", "run", "python", "scripts/check.py", "e2e")),
    )


def select_steps(skip_e2e: bool) -> list[tuple[str, tuple[str, ...]]]:
    """Return the steps to run for this invocation."""
    return [step for step in verification_steps() if not (skip_e2e and step[0] == "e2e")]


def run_steps(
    destination: Path, steps: Sequence[tuple[str, tuple[str, ...]]]
) -> list[tuple[str, int]]:
    """Run each step inside the export, stopping at the first failure."""
    results: list[tuple[str, int]] = []
    for label, command in steps:
        print(f"+ [{label}] {' '.join(command)}", flush=True)
        completed = subprocess.run(resolve_command(command), cwd=destination)
        results.append((label, completed.returncode))
        if completed.returncode != 0:
            break
    return results


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse clean-check options."""
    parser = argparse.ArgumentParser(description=SUMMARY)
    parser.add_argument(
        "--skip-e2e",
        action="store_true",
        help="Run every step except the deterministic browser journey.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Verify the current working tree in an isolated temporary export."""
    args = parse_args(argv)
    with tempfile.TemporaryDirectory(prefix=EXPORT_PREFIX) as directory:
        destination = Path(directory)
        copied = export_repository(destination)
        print(f"Exported {copied} file(s) into an isolated temporary repository.")
        initialize_repository(destination)
        results = run_steps(destination, select_steps(args.skip_e2e))

    print("clean-check summary:")
    for label, code in results:
        print(f"  {label}: {'PASS' if code == 0 else f'FAIL (exit {code})'}")
    return 0 if all(code == 0 for _, code in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
