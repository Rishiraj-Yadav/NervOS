"""Tests for the Stage A1 repository command facade."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_development_module() -> ModuleType:
    """Load the repository development script without requiring it as a package."""
    path = ROOT / "scripts" / "dev.py"
    spec = importlib.util.spec_from_file_location("nervos_dev", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load development script at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_script(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a repository script with the active test interpreter."""
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def run_hook(path: str) -> dict[str, object] | None:
    """Run a Claude hook with a representative Read payload."""
    result = subprocess.run(
        [sys.executable, ".claude/hooks/scripts/pretool_sensitive_read.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        input=f'{{"tool_input":{{"file_path":"{path}"}}}}',
        text=True,
    )

    assert result.returncode == 0
    if not result.stdout:
        return None

    import json

    return json.loads(result.stdout)


def test_sensitive_read_hook_allows_env_example() -> None:
    assert run_hook(".env.example") is None


def test_sensitive_read_hook_denies_real_env_file() -> None:
    output = run_hook(".env.local")

    assert output is not None
    hook_output = output["hookSpecificOutput"]
    assert isinstance(hook_output, dict)
    assert hook_output["permissionDecision"] == "deny"


def test_sensitive_read_hook_denies_nested_secrets() -> None:
    output = run_hook("data/secrets/credentials.json")

    assert output is not None
    hook_output = output["hookSpecificOutput"]
    assert isinstance(hook_output, dict)
    assert hook_output["permissionDecision"] == "deny"


def test_check_script_lists_supported_groups() -> None:
    result = run_script("scripts/check.py", "--help")

    assert result.returncode == 0
    assert "{lint,typecheck,test,check}" in result.stdout


def test_development_api_migrates_before_starting_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from nervos_api.config import Settings

    dev = load_development_module()

    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == ROOT
        assert check is False
        assert env["NERVOS_ENVIRONMENT"] == "test"
        assert env["NERVOS_DATABASE_PATH"] == str((tmp_path / "launcher.db").resolve())
        assert env["NERVOS_APP_ORIGIN"] == "https://localhost:8443"
        assert env["NERVOS_LOG_LEVEL"] == "WARNING"
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    settings = Settings(
        environment="test",
        database_path=tmp_path / "launcher.db",
        app_origin="https://localhost:8443",
        log_level="WARNING",
    )

    assert dev.run_api(settings) == 0
    assert commands[0][-2:] == ["upgrade", "head"]
    assert commands[1][2:4] == ["uvicorn", "nervos_api.main:app"]
    assert commands[1][-2:] == ["--log-level", "warning"]


def test_development_api_stops_when_migration_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from nervos_api.config import Settings

    dev = load_development_module()

    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del cwd, check
        assert env["NERVOS_ENVIRONMENT"] == "test"
        assert env["NERVOS_DATABASE_PATH"] == str((tmp_path / "launcher.db").resolve())
        commands.append(command)
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(dev.subprocess, "run", fake_run)

    assert (
        dev.run_api(
            Settings(
                environment="test",
                database_path=tmp_path / "launcher.db",
                app_origin="https://localhost:8443",
            )
        )
        == 7
    )
    assert len(commands) == 1
    assert commands[0][-2:] == ["upgrade", "head"]


def test_development_script_reports_deferred_web() -> None:
    result = run_script("scripts/dev.py", "web")

    assert result.returncode == 2
    assert "not implemented in A2" in result.stdout
    assert "milestone A4" in result.stdout
