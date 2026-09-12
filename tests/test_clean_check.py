"""Regression tests for the isolated clean-environment verification export."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_clean_check() -> ModuleType:
    path = ROOT / "scripts" / "clean_check.py"
    spec = importlib.util.spec_from_file_location("nervos_clean_check", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load clean-check script at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def checkout_fingerprint() -> tuple[str, str]:
    """Capture the observable state of the checkout under test.

    ``HEAD`` may not exist when this runs inside a clean-check export, which is
    deliberately staged without a commit, so a missing HEAD is not an error.
    """
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return status, head.stdout.strip()


def test_export_does_not_mutate_the_active_checkout(tmp_path: Path) -> None:
    clean_check = load_clean_check()
    before = checkout_fingerprint()
    destination = tmp_path / "export"
    destination.mkdir()

    copied = clean_check.export_repository(destination)
    clean_check.initialize_repository(destination)

    assert copied > 0
    assert checkout_fingerprint() == before


def test_exported_file_set_matches_git_enumeration(tmp_path: Path) -> None:
    clean_check = load_clean_check()
    destination = tmp_path / "export"
    destination.mkdir()

    copied = clean_check.export_repository(destination)

    expected = sorted(
        relative
        for relative in clean_check.repository_files()
        if (clean_check.ROOT / relative).is_file()
    )
    actual = sorted(
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    )
    assert copied == len(expected)
    assert actual == expected


def test_initialize_repository_needs_no_git_identity_and_creates_no_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean_check = load_clean_check()
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("hello\n", encoding="utf-8")

    empty_config = tmp_path / "empty-gitconfig"
    empty_config.write_text("", encoding="utf-8")
    # Remove every source of Git author identity: global/system config and environment.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty_config))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "")

    clean_check.initialize_repository(source)

    staged = subprocess.run(
        ["git", "ls-files", "--cached"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert staged.split() == ["file.txt"]

    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=source,
        capture_output=True,
    )
    assert head.returncode != 0


def test_repository_files_excludes_ignored_paths(tmp_path: Path) -> None:
    clean_check = load_clean_check()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (tmp_path / "kept.txt").write_text("x\n", encoding="utf-8")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "secret.txt").write_text("x\n", encoding="utf-8")

    assert clean_check.repository_files(root=tmp_path) == [".gitignore", "kept.txt"]


def test_skip_e2e_removes_only_the_browser_step() -> None:
    clean_check = load_clean_check()

    full = [label for label, _ in clean_check.select_steps(skip_e2e=False)]
    reduced = [label for label, _ in clean_check.select_steps(skip_e2e=True)]

    assert "e2e" in full
    assert reduced == [label for label in full if label != "e2e"]


def test_verification_steps_use_repository_owned_commands() -> None:
    clean_check = load_clean_check()
    steps = dict(clean_check.verification_steps())

    assert steps["lockfile"] == ("uv", "lock", "--check")
    assert steps["check"] == ("uv", "run", "python", "scripts/check.py", "check")
    assert steps["e2e"] == ("uv", "run", "python", "scripts/check.py", "e2e")
