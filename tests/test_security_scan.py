"""Tests for the tracked-file security scanner."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Sensitive-looking fixture values are assembled at runtime from concatenated
# fragments so this tracked test file never contains a literal that the scanner
# would flag. None of these values can authenticate to any service.
PRIVATE_KEY = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
ANTHROPIC_KEY = "sk-ant-" + "a" * 24
OPENAI_KEY = "sk-" + "a" * 40
GITHUB_TOKEN = "ghp_" + "a" * 36
AWS_KEY_ID = "AKIA" + "A" * 16
SLACK_TOKEN = "xoxb-" + "a" * 12
GOOGLE_API_KEY = "AIza" + "a" * 35
SENTINEL = "SENTINELDONOTPRINT"

CONTENT_SAMPLES = {
    "PRIVATE_KEY_BLOCK": PRIVATE_KEY,
    "ANTHROPIC_KEY": ANTHROPIC_KEY,
    "OPENAI_STYLE_KEY": OPENAI_KEY,
    "GITHUB_TOKEN": GITHUB_TOKEN,
    "AWS_ACCESS_KEY_ID": AWS_KEY_ID,
    "SLACK_TOKEN": SLACK_TOKEN,
    "GOOGLE_API_KEY": GOOGLE_API_KEY,
}


def load_module() -> ModuleType:
    path = ROOT / "scripts" / "security_scan.py"
    spec = importlib.util.spec_from_file_location("nervos_security_scan", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load scanner at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Point a fresh scanner module at an isolated temporary Git repository."""
    module = load_module()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    return module


def rule_ids(findings: list[Any]) -> list[str]:
    return [finding.rule_id for finding in findings]


# --- content rules ---------------------------------------------------------


@pytest.mark.parametrize("rule_id", sorted(CONTENT_SAMPLES))
def test_detects_each_content_rule(rule_id: str) -> None:
    scanner = load_module()
    value = CONTENT_SAMPLES[rule_id]

    findings = scanner.content_findings("example.py", f'credential = "{value}"')

    assert rule_id in rule_ids(findings)


def test_content_finding_reports_line_number_without_matched_value() -> None:
    scanner = load_module()

    findings = scanner.content_findings("example.py", f"first\nkey = {AWS_KEY_ID}\nthird")

    assert len(findings) == 1
    assert findings[0].line == 2
    assert AWS_KEY_ID not in findings[0].render()


def test_ignores_benign_credential_vocabulary() -> None:
    scanner = load_module()
    benign = (
        "password = get_password()\n"
        'api_key = "not-a-real-key"\n'
        "token_name = 'session'\n"
        "secret = compute_secret()\n"
        "# Policy forbids committing API keys, access tokens and auth headers.\n"
    )

    assert scanner.content_findings("benign.py", benign) == []


def test_ignores_stage_a_end_to_end_credentials() -> None:
    scanner = load_module()
    spec_text = (
        'const username = "Stage-A.Admin";\n'
        'const canonicalUsername = "stage-a.admin";\n'
        'const password = "StageA test password 2026!";\n'
    )

    assert scanner.content_findings("stage-a.spec.ts", spec_text) == []


def test_placeholder_values_are_suppressed() -> None:
    scanner = load_module()
    # Matches the AWS key shape, but carries an unmistakable placeholder token.
    placeholder = "AKIA" + "PLACEHOLDER" + "AAAAA"

    assert scanner.content_findings("docs.md", f"example: {placeholder}") == []


def test_allow_marker_suppresses_only_the_named_rule() -> None:
    scanner = load_module()
    waived = f"key = {AWS_KEY_ID}  # security-scan: allow AWS_ACCESS_KEY_ID"
    other = f"key = {GITHUB_TOKEN}  # security-scan: allow AWS_ACCESS_KEY_ID"

    assert scanner.content_findings("a.py", waived) == []
    assert "GITHUB_TOKEN" in rule_ids(scanner.content_findings("a.py", other))


# --- path rules ------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "rule_id"),
    [
        (".env", "FORBIDDEN_ENV"),
        (".env.local", "FORBIDDEN_ENV"),
        ("config/secrets/token.txt", "FORBIDDEN_SECRETS_DIR"),
        ("deploy/id_rsa", "FORBIDDEN_KEY_FILE"),
        ("certs/server.pem", "FORBIDDEN_KEY_FILE"),
        ("data/nervos.db", "FORBIDDEN_DATABASE"),
        ("data/nervos.db-wal", "FORBIDDEN_DATABASE"),
        ("tmp/service.log", "FORBIDDEN_LOG"),
        ("apps/web/playwright-report/index.html", "FORBIDDEN_REPORT"),
        ("apps/web/test-results/trace.zip", "FORBIDDEN_REPORT"),
        ("node_modules/pkg/index.js", "FORBIDDEN_VENDOR"),
        (".mcp.json", "FORBIDDEN_LOCAL_STATE"),
        (".serena/project.yml", "FORBIDDEN_LOCAL_STATE"),
    ],
)
def test_flags_forbidden_paths(path: str, rule_id: str) -> None:
    scanner = load_module()

    assert rule_id in rule_ids(scanner.path_findings(scanner.PurePosixPath(path)))


def test_allows_approved_exceptions() -> None:
    scanner = load_module()

    assert scanner.path_findings(scanner.PurePosixPath(".env.example")) == []


def test_path_findings_ignore_allow_markers() -> None:
    scanner = load_module()
    text = "# security-scan: allow FORBIDDEN_ENV\ncredentials = 1\n"

    assert scanner.content_findings(".env", text) == []
    assert "FORBIDDEN_ENV" in rule_ids(scanner.path_findings(scanner.PurePosixPath(".env")))


# --- file-level scanning ---------------------------------------------------


def test_sqlite_header_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scanner = build_repository(tmp_path, monkeypatch)
    (tmp_path / "misnamed.txt").write_bytes(scanner.SQLITE_MAGIC + b"\x00" * 64)

    assert "SQLITE_FILE_HEADER" in rule_ids(scanner.scan_file("misnamed.txt"))


def test_binary_and_oversize_content_is_skipped_but_path_rules_still_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanner = build_repository(tmp_path, monkeypatch)
    (tmp_path / "blob.bin").write_bytes(b"\x00" * 32 + AWS_KEY_ID.encode())
    (tmp_path / "big.log").write_text(GITHUB_TOKEN + "\n" + "x" * (scanner.MAX_CONTENT_BYTES + 1))

    assert rule_ids(scanner.scan_file("blob.bin")) == []
    # The oversize file's secret is not read, but its forbidden path is still reported.
    assert rule_ids(scanner.scan_file("big.log")) == ["FORBIDDEN_LOG"]


# --- repository-level behaviour -------------------------------------------


def test_scan_reports_findings_and_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scanner = build_repository(tmp_path, monkeypatch)
    (tmp_path / "leak.py").write_text(f'key = "{AWS_KEY_ID}"\n')

    assert scanner.main([]) == 1
    captured = capsys.readouterr()
    assert "AWS_ACCESS_KEY_ID" in captured.out


def test_scan_exits_zero_when_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scanner = build_repository(tmp_path, monkeypatch)
    (tmp_path / "clean.py").write_text("value = 1\n")

    assert scanner.main([]) == 0
    assert "no findings" in capsys.readouterr().out


def test_matched_value_never_appears_in_scanner_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scanner = build_repository(tmp_path, monkeypatch)
    # The sentinel sits on the very line that matches, so any line echo would leak it.
    (tmp_path / "leak.py").write_text(f'key = "{AWS_KEY_ID}"  # {SENTINEL}\n')

    exit_code = scanner.main([])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert SENTINEL not in captured.out
    assert SENTINEL not in captured.err
    assert AWS_KEY_ID not in captured.out
    assert AWS_KEY_ID not in captured.err


def test_tracked_only_excludes_untracked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanner = build_repository(tmp_path, monkeypatch)
    (tmp_path / "notes.txt").write_text("hello\n")

    assert "notes.txt" in scanner.list_repository_files(tracked_only=False)
    assert "notes.txt" not in scanner.list_repository_files(tracked_only=True)


def test_scan_never_lists_files_outside_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanner = build_repository(tmp_path, monkeypatch)
    (tmp_path / ".gitignore").write_text("ignored/\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "leak.py").write_text(f'key = "{AWS_KEY_ID}"\n')

    assert scanner.list_repository_files(tracked_only=False) == [".gitignore"]


# --- regression guards -----------------------------------------------------


def test_rule_ids_are_unique() -> None:
    scanner = load_module()
    identifiers = [rule.rule_id for rule in scanner.PATH_RULES]
    identifiers += [rule.rule_id for rule in scanner.CONTENT_RULES]

    assert len(identifiers) == len(set(identifiers))


def test_scanner_definition_files_do_not_self_match() -> None:
    """The scanner and its own pattern-bearing hooks must not trip their own rules."""
    scanner = load_module()
    definition_files = [
        "scripts/security_scan.py",
        "tests/test_security_scan.py",
        ".claude/hooks/scripts/prewrite_secret_scan.py",
    ]

    for relative_path in definition_files:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        assert scanner.content_findings(relative_path, text) == []
