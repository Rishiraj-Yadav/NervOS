from __future__ import annotations

import json
import re
import sys
from pathlib import PurePath

SENSITIVE_NAMES = {
    ".env",
    "credentials.json",
    "secrets.json",
    "id_rsa",
    "id_ed25519",
}

SENSITIVE_PARTS = {"secrets", ".ssh"}

# Deliberately high-confidence patterns to minimize false positives.
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "Anthropic-style API key"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b"), "secret API key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "GitHub token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
]


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool_input = payload.get("tool_input") or {}
    file_path = str(tool_input.get("file_path") or tool_input.get("path") or "")

    if file_path:
        path = PurePath(file_path)
        parts = {part.lower() for part in path.parts}
        name = path.name.lower()

        if name.startswith(".env") and name != ".env.example":
            deny(f"Direct write to {file_path} is blocked. Use .env.example with fake values.")
            return 0

        if name in SENSITIVE_NAMES or parts.intersection(SENSITIVE_PARTS):
            deny(f"Writing to sensitive file/path {file_path} is blocked by NervOS policy.")
            return 0

    content_parts: list[str] = []
    for key in ("content", "new_string"):
        value = tool_input.get(key)
        if isinstance(value, str):
            content_parts.append(value)
    content = "\n".join(content_parts)

    for pattern, label in SECRET_PATTERNS:
        if pattern.search(content):
            deny(
                f"Potential {label} detected in content. Use a fake example value or "
                "secret-manager reference rather than writing the credential."
            )
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
