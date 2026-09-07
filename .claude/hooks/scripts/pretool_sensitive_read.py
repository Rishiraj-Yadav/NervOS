from __future__ import annotations

import json
import sys
from pathlib import PurePath
from typing import Any, cast


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
        payload = cast(dict[str, Any], json.load(sys.stdin))
    except (json.JSONDecodeError, OSError):
        return 0

    tool_input = payload.get("tool_input") or {}
    file_path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    if not file_path:
        return 0

    path = PurePath(file_path)
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()

    if name.startswith(".env") and name != ".env.example":
        deny(f"Reading {file_path} is blocked. Only .env.example contains safe fake values.")
    elif parts.intersection({"secrets", ".ssh"}):
        deny(f"Reading sensitive path {file_path} is blocked by NervOS policy.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
