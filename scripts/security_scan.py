"""Detect tracked sensitive or forbidden artifacts without ever printing a match.

The scan is scoped by Git, never by a filesystem walk: it enumerates the files Git
considers part of the repository (tracked files, plus untracked files that are not
ignored) so a problem is caught before it is staged. ``--tracked-only`` restricts
the scan to already-tracked files for CI use.

Findings never include a matched value, the matched line, or surrounding context.
Each finding reports only a path, an optional line number, a rule identifier, and a
category. Path rules are structural and cannot be suppressed; content rules can be
waived per line with an explicit ``security-scan: allow RULE-ID`` marker.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = "Scan tracked and untracked repository files for secrets and forbidden artifacts."
MAX_CONTENT_BYTES = 1_048_576
BINARY_SNIFF_BYTES = 8192
SQLITE_MAGIC = b"SQLite format 3\x00"
ALLOW_MARKER = re.compile(r"security-scan:\s*allow\s+([A-Z0-9_]+)")
PLACEHOLDER = re.compile(
    r"example|change[_-]?me|placeholder|redacted|dummy|fake|sample|your[_-]|<[^>]*>|x{4,}",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    """One safe, non-secret-bearing scan result."""

    path: str
    rule_id: str
    category: str
    message: str
    line: int | None = None

    def render(self) -> str:
        """Render a finding without exposing any matched content."""
        location = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"{location}: {self.rule_id} [{self.category}] {self.message}"


@dataclass(frozen=True)
class PathRule:
    """A structural rule over a repository-relative path."""

    rule_id: str
    category: str
    message: str
    matches: Callable[[PurePosixPath], bool]


@dataclass(frozen=True)
class ContentRule:
    """An anchored rule over the text of one file."""

    rule_id: str
    category: str
    message: str
    pattern: re.Pattern[str]


def _is_env_file(path: PurePosixPath) -> bool:
    return path.name.startswith(".env") and path.name != ".env.example"


def _is_under_secrets(path: PurePosixPath) -> bool:
    return any(part in {"secrets", ".ssh"} for part in path.parts)


def _is_key_file(path: PurePosixPath) -> bool:
    return path.name in {"id_rsa", "id_ed25519"} or path.suffix.lower() in {
        ".pem",
        ".key",
        ".p12",
        ".pfx",
    }


def _is_database(path: PurePosixPath) -> bool:
    return (
        path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
        or path.name.endswith((".db-shm", ".db-wal"))
        or ".nervos" in path.parts
    )


def _is_report(path: PurePosixPath) -> bool:
    return bool(
        set(path.parts)
        & {"playwright-report", "test-results", "blob-report", "htmlcov", "coverage"}
    ) or path.name.startswith(".coverage")


def _is_vendor(path: PurePosixPath) -> bool:
    return bool(set(path.parts) & {"node_modules", ".venv", "__pycache__"}) or path.suffix == ".pyc"


def _is_local_state(path: PurePosixPath) -> bool:
    return (
        path.as_posix() in {".mcp.json", ".claude/settings.local.json"}
        or bool(set(path.parts) & {".vscode", ".idea", ".serena"})
        or path.name in {".DS_Store", "Thumbs.db", "Desktop.ini"}
    )


PATH_RULES: tuple[PathRule, ...] = (
    PathRule("FORBIDDEN_ENV", "secrets", "environment file must not be tracked", _is_env_file),
    PathRule(
        "FORBIDDEN_SECRETS_DIR",
        "secrets",
        "secrets directory must not be tracked",
        _is_under_secrets,
    ),
    PathRule(
        "FORBIDDEN_KEY_FILE", "secrets", "private key material must not be tracked", _is_key_file
    ),
    PathRule("FORBIDDEN_DATABASE", "data", "database file must not be tracked", _is_database),
    PathRule(
        "FORBIDDEN_LOG",
        "data",
        "log file must not be tracked",
        lambda p: p.suffix.lower() == ".log",
    ),
    PathRule("FORBIDDEN_REPORT", "generated", "generated report must not be tracked", _is_report),
    PathRule(
        "FORBIDDEN_VENDOR", "generated", "vendored dependency tree must not be tracked", _is_vendor
    ),
    PathRule(
        "FORBIDDEN_LOCAL_STATE",
        "local-state",
        "local tool state must not be tracked",
        _is_local_state,
    ),
)

CONTENT_RULES: tuple[ContentRule, ...] = (
    ContentRule(
        "PRIVATE_KEY_BLOCK",
        "private-key",
        "private key block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    ),
    ContentRule(
        "ANTHROPIC_KEY",
        "credential",
        "Anthropic-style API key",
        re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    ),
    ContentRule(
        "OPENAI_STYLE_KEY", "credential", "secret API key", re.compile(r"\bsk-[A-Za-z0-9]{32,}")
    ),
    ContentRule(
        "GITHUB_TOKEN", "credential", "GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")
    ),
    ContentRule(
        "AWS_ACCESS_KEY_ID", "credential", "AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")
    ),
    ContentRule(
        "SLACK_TOKEN", "credential", "Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")
    ),
    ContentRule(
        "GOOGLE_API_KEY", "credential", "Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")
    ),
)


def list_repository_files(tracked_only: bool) -> list[str]:
    """Return repository-relative paths that Git considers part of the repository."""
    git = shutil.which("git")
    if git is None:
        raise SystemExit("Required command 'git' was not found.")
    command = [git, "ls-files", "-z", "--cached"]
    if not tracked_only:
        command += ["--others", "--exclude-standard"]
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return sorted(
        entry.decode("utf-8", errors="surrogateescape")
        for entry in result.stdout.split(b"\x00")
        if entry
    )


def path_findings(path: PurePosixPath) -> list[Finding]:
    """Apply the structural path rules."""
    location = path.as_posix()
    return [
        Finding(location, rule.rule_id, rule.category, rule.message)
        for rule in PATH_RULES
        if rule.matches(path)
    ]


def _allowed_rules(lines: Sequence[str], index: int) -> set[str]:
    """Collect rule ids waived on this line or the line directly above it."""
    allowed: set[str] = set()
    for candidate in (index - 1, index):
        if 0 <= candidate < len(lines):
            allowed.update(ALLOW_MARKER.findall(lines[candidate]))
    return allowed


def content_findings(location: str, text: str) -> list[Finding]:
    """Apply the anchored content rules to one decoded text file."""
    findings: list[Finding] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        for rule in CONTENT_RULES:
            match = rule.pattern.search(line)
            if match is None:
                continue
            if PLACEHOLDER.search(match.group(0)):
                continue
            if rule.rule_id in _allowed_rules(lines, index):
                continue
            findings.append(Finding(location, rule.rule_id, rule.category, rule.message, index + 1))
    return findings


def scan_file(relative_path: str) -> list[Finding]:
    """Scan one repository file, in path-rule then content-rule order."""
    path = PurePosixPath(relative_path)
    findings = path_findings(path)
    absolute = ROOT / relative_path
    try:
        size = absolute.stat().st_size
        with absolute.open("rb") as handle:
            head = handle.read(BINARY_SNIFF_BYTES)
    except OSError:
        return findings

    if head.startswith(SQLITE_MAGIC):
        findings.append(
            Finding(relative_path, "SQLITE_FILE_HEADER", "data", "SQLite database content")
        )
    if b"\x00" in head or size > MAX_CONTENT_BYTES:
        return findings
    try:
        text = absolute.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return findings
    findings.extend(content_findings(relative_path, text))
    return findings


def scan(paths: Sequence[str]) -> list[Finding]:
    """Scan every listed path and return findings ordered by path then line."""
    findings: list[Finding] = []
    for relative_path in paths:
        findings.extend(scan_file(relative_path))
    return sorted(findings, key=lambda item: (item.path, item.line or 0, item.rule_id))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse scanner options."""
    parser = argparse.ArgumentParser(description=SUMMARY)
    parser.add_argument(
        "--tracked-only",
        action="store_true",
        help="Scan only tracked files; by default untracked, non-ignored files are included too.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Scan the repository and report safe findings without echoing any match."""
    args = parse_args(argv)
    try:
        paths = list_repository_files(tracked_only=args.tracked_only)
        findings = scan(paths)
    except (subprocess.CalledProcessError, SystemExit) as error:
        print(f"security scan failed: {error}", file=sys.stderr)
        return 2

    for finding in findings:
        print(finding.render())
    if findings:
        print(
            f"security scan: {len(findings)} finding(s) across {len(paths)} file(s)",
            file=sys.stderr,
        )
        return 1
    print(f"security scan: {len(paths)} file(s) scanned, no findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
