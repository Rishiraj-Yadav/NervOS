from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    cwd = Path(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    status_path = cwd / "docs" / "implementation-status.md"

    if not status_path.exists():
        context = (
            "NervOS status file is missing. Do not assume implementation phases "
            "are complete; inspect the repository before making architectural claims."
        )
    else:
        text = status_path.read_text(encoding="utf-8", errors="replace")
        lines = [line.rstrip() for line in text.splitlines()]
        selected: list[str] = []
        capture = False
        for line in lines:
            if line.startswith("## Current phase"):
                capture = True
                selected.append(line)
                continue
            if capture and line.startswith("## "):
                break
            if capture and line.strip():
                selected.append(line)

        phase = "\n".join(selected[:10]).strip()
        context = (
            "Current NervOS implementation state from docs/implementation-status.md:\n"
            + (phase or "Read the status file before significant work.")
        )

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
