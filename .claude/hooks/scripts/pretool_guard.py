from __future__ import annotations

import json
import re
import sys


DANGEROUS_PATTERNS = [
    (
        re.compile(r"\bgit\s+push\b.*(?:--force|-f)(?:\s|$)", re.I),
        "Force-push is blocked by NervOS project policy.",
    ),
    (
        re.compile(r"\bgit\s+reset\s+--hard\b", re.I),
        "git reset --hard is blocked by NervOS project policy.",
    ),
    (
        re.compile(r"\bgit\s+clean\s+-[^\n]*f", re.I),
        "Destructive git clean is blocked by NervOS project policy.",
    ),
    (
        re.compile(r"\brm\s+-[^\n]*r[^\n]*f[^\n]*\s+[/~](?:\s|$)", re.I),
        "Broad recursive forced deletion is blocked.",
    ),
    (
        re.compile(r"\bRemove-Item\b[^\n]*-Recurse[^\n]*-Force", re.I),
        "Recursive forced PowerShell deletion requires explicit manual handling.",
    ),
    (
        re.compile(r"\bmkfs(?:\.\w+)?\b", re.I),
        "Filesystem formatting commands are blocked.",
    ),
    (
        re.compile(r"\bformat\s+[A-Z]:", re.I),
        "Drive formatting commands are blocked.",
    ),
]


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool_input = payload.get("tool_input") or {}
    command = str(tool_input.get("command") or "")

    for pattern, reason in DANGEROUS_PATTERNS:
        if pattern.search(command):
            deny(reason)
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
