"""Tests for the Stage A1 repository command facade."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script_module(name: str, relative_path: str) -> ModuleType:
    """Load a repository script without requiring it as a package."""
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load repository script at {path}")
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
    assert "{lint,typecheck,test,security,e2e,check}" in result.stdout


def test_bootstrap_installs_locked_dependencies_then_chromium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = load_script_module("nervos_bootstrap", "scripts/bootstrap.py")
    commands: list[list[str]] = []

    def fake_prerequisites() -> tuple[str, str]:
        return "uv-bin", "pnpm-bin"

    def record_command(command: list[str]) -> None:
        commands.append(command)

    monkeypatch.setattr(bootstrap, "validate_prerequisites", fake_prerequisites)
    monkeypatch.setattr(bootstrap, "run", record_command)

    assert bootstrap.main([]) == 0
    assert commands == [
        ["uv-bin", "sync", "--frozen", "--all-packages"],
        ["pnpm-bin", "install", "--frozen-lockfile"],
        [
            "pnpm-bin",
            "--dir",
            "apps/web",
            "exec",
            "playwright",
            "install",
            "chromium",
        ],
    ]


def test_bootstrap_skip_browser_installs_no_chromium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = load_script_module("nervos_bootstrap_skip", "scripts/bootstrap.py")
    commands: list[list[str]] = []

    def fake_prerequisites() -> tuple[str, str]:
        return "uv-bin", "pnpm-bin"

    def record_command(command: list[str]) -> None:
        commands.append(command)

    monkeypatch.setattr(bootstrap, "validate_prerequisites", fake_prerequisites)
    monkeypatch.setattr(bootstrap, "run", record_command)

    assert bootstrap.main(["--skip-browser"]) == 0
    assert commands == [
        ["uv-bin", "sync", "--frozen", "--all-packages"],
        ["pnpm-bin", "install", "--frozen-lockfile"],
    ]


def test_bootstrap_rejects_outdated_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap = load_script_module("nervos_bootstrap_uv", "scripts/bootstrap.py")

    def fake_read_version(command: str, *arguments: str) -> tuple[int, ...]:
        versions = {"uv": (0, 9, 0), "node": (22, 12, 0), "pnpm": (10, 0, 0)}
        return versions[Path(command).name.replace("-bin", "")]

    def fake_require_command(name: str) -> str:
        return f"{name}-bin"

    monkeypatch.setattr(bootstrap, "require_command", fake_require_command)
    monkeypatch.setattr(bootstrap, "read_version", fake_read_version)

    with pytest.raises(SystemExit, match="requires uv"):
        bootstrap.validate_prerequisites()


def test_development_api_migrates_before_starting_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from nervos_api.config import Settings

    dev = load_script_module("nervos_dev", "scripts/dev.py")

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

    dev = load_script_module("nervos_dev", "scripts/dev.py")

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


def test_development_web_runs_root_pnpm_script_and_propagates_exit_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dev = load_script_module("nervos_dev", "scripts/dev.py")
    resolved_pnpm = str(ROOT / "tools" / "pnpm.cmd")

    def fake_which(command: str) -> str | None:
        assert command == "pnpm"
        return resolved_pnpm

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        check: bool,
        shell: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert command == [resolved_pnpm, "dev:web"]
        assert cwd == ROOT
        assert check is False
        assert shell is False
        return subprocess.CompletedProcess(command, 9)

    monkeypatch.setattr(dev.shutil, "which", fake_which)
    monkeypatch.setattr(dev.subprocess, "run", fake_run)

    assert dev.run_web() == 9


def test_development_web_requires_resolved_pnpm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dev = load_script_module("nervos_dev", "scripts/dev.py")

    def missing_command(command: str) -> None:
        assert command == "pnpm"
        return None

    monkeypatch.setattr(dev.shutil, "which", missing_command)

    with pytest.raises(SystemExit, match="Required command 'pnpm' was not found"):
        dev.run_web()


WORKFLOW_DIR = ROOT / ".github" / "workflows"


def read_workflow(name: str) -> str:
    return (WORKFLOW_DIR / name).read_text(encoding="utf-8")


def test_workflows_replace_the_stage_a_placeholders() -> None:
    for name in ("ci.yml", "security.yml"):
        text = read_workflow(name)
        assert "intentionally deferred" not in text
        assert "jobs:" in text


def test_workflows_are_read_only_and_never_reference_secrets() -> None:
    import re

    write_scope = re.compile(r"^\s{2,}[A-Za-z-]+:\s*write\s*$", re.MULTILINE)
    for name in ("ci.yml", "security.yml"):
        text = read_workflow(name)
        assert "permissions:" in text
        assert "contents: read" in text
        assert write_scope.search(text) is None
        assert "${{ secrets" not in text
        assert "pull_request_target" not in text


def test_actions_are_pinned_to_full_length_commit_shas() -> None:
    import re

    reference = re.compile(r"uses:\s*([^\s#]+)")
    pinned = re.compile(r"^[^@]+@[0-9a-f]{40}$")
    for name in ("ci.yml", "security.yml"):
        references = reference.findall(read_workflow(name))
        assert references, f"{name} declares no action"
        for reference_text in references:
            assert pinned.match(reference_text), f"{name}: unpinned action {reference_text}"


def test_every_pinned_action_carries_a_release_label() -> None:
    for name in ("ci.yml", "security.yml"):
        for line in read_workflow(name).splitlines():
            if "uses:" in line:
                assert "# v" in line, f"{name}: missing release label on {line.strip()}"


def test_ci_workflow_invokes_repository_commands() -> None:
    text = read_workflow("ci.yml")

    assert "uv lock --check" in text
    assert "scripts/bootstrap.py --skip-browser" in text
    assert "scripts/check.py check" in text
    assert "scripts/check.py e2e" in text


def test_ci_check_job_never_provisions_playwright() -> None:
    check_job = read_workflow("ci.yml").split("  e2e:")[0]

    assert "--skip-browser" in check_job
    assert "playwright install" not in check_job.lower()
    assert "install-deps" not in check_job


def test_security_workflow_uses_the_repository_scanner() -> None:
    text = read_workflow("security.yml")

    assert "scripts/security_scan.py --tracked-only" in text
    assert "pull_request:" in text
    assert "schedule:" in text
    # No secret pattern may be duplicated in workflow YAML.
    for token in ("AKIA", "ghp_", "sk-ant-", "PRIVATE KEY"):
        assert token not in text


def test_check_group_includes_security_and_excludes_e2e() -> None:
    check = load_script_module("nervos_check_composition", "scripts/check.py")

    assert check.CHECKS["check"] == (
        check.CHECKS["lint"]
        + check.CHECKS["typecheck"]
        + check.CHECKS["test"]
        + check.CHECKS["security"]
    )
    flattened = " ".join(" ".join(command) for command in check.CHECKS["check"])
    assert "scripts/security_scan.py" in flattened
    assert "e2e" not in flattened


def test_gitignore_treats_graphify_cache_as_local_state() -> None:
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "graphify-out/cache/" in text
    # The generated report itself remains a tracked project artifact.
    assert "graphify-out/graph.json" not in text


def test_node_version_file_stays_inside_the_declared_engine_range() -> None:
    import json

    declared = (ROOT / ".node-version").read_text(encoding="utf-8").strip()
    engines = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["engines"]

    assert engines["node"] == ">=22.12.0 <25"
    assert int(declared.split(".")[0]) == 24
