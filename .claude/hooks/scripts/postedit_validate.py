from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def add_context(message: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": message,
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
    raw_path = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return 0

    path = Path(raw_path)
    if not path.is_absolute():
        path = Path(payload.get("cwd") or os.getcwd()) / path

    if not path.exists() or not path.is_file():
        return 0

    try:
        if path.suffix == ".py":
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
        elif path.suffix == ".json":
            json.loads(path.read_text(encoding="utf-8"))
        else:
            return 0
    except Exception as exc:
        add_context(
            f"Fast post-edit validation failed for {path}: {exc}. "
            "Fix the syntax/format before considering this change complete."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
