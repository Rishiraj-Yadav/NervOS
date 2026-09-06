# NervOS Claude Code Hooks

## What hooks are

Hooks are deterministic handlers that Claude Code runs automatically at specific lifecycle events.

They are different from skills:

- **Skill**: instructions that tell Claude how to perform a repeatable workflow.
- **Hook**: code that Claude Code automatically executes when an event occurs.

Useful hook events include:

- `SessionStart` — when a Claude Code session starts/resumes
- `PreToolUse` — immediately before Claude uses a tool; can block dangerous operations
- `PostToolUse` — after a successful tool call; useful for fast validation
- `Stop` — when Claude is about to finish a turn

Project hook configuration belongs in `.claude/settings.json`. Hook scripts can live anywhere; NervOS keeps them under `.claude/hooks/scripts/`.

## Hooks included

### `session_context.py`

Event: `SessionStart`

Reads `docs/implementation-status.md` and injects a short current-phase summary into Claude's context. This helps a fresh/resumed Claude session remember that Stage A must not accidentally implement Stage B+ features.

### `pretool_guard.py`

Event: `PreToolUse` for Bash/PowerShell.

Blocks a deliberately small set of obviously destructive commands such as force-push, hard reset, broad recursive forced deletion, and drive/filesystem formatting.

This complements Claude Code permission rules; it does not replace them.

### `prewrite_secret_scan.py`

Event: `PreToolUse` for Edit/Write.

Blocks writes to common secret/private-key paths and catches a few high-confidence real-secret patterns before content is written.

It intentionally avoids an enormous generic regex list because false positives would make normal development unusable.

### `postedit_validate.py`

Event: `PostToolUse` for Edit/Write.

Performs extremely fast validation only:

- Python file -> syntax compile check
- JSON file -> JSON parse check

If validation fails it adds context telling Claude to fix the file.

It does NOT replace `pytest`, Ruff, Pyright, frontend tests, TypeScript checks, or E2E.

## Hook design rules for NervOS

Hooks should be:

- fast
- deterministic
- understandable
- repository-safe
- cross-platform where practical

Do not run the entire test suite after every edit. Full checks belong in milestone prompts, skills, CI, and release checks.

## Windows

The supplied settings invoke `python`.

If your machine uses `py` instead, change the hook command in `.claude/settings.json` from `python` to `py`.

## When to add more hooks

Only add a hook when a deterministic automatic rule provides clear value.

Good future examples:

- validating agent package manifests after package edits
- preventing direct edits to generated schema files
- enforcing package-signing checks before a release command

Bad examples:

- asking an LLM to review every tiny edit
- running all backend/frontend/E2E tests after each write
- automatically committing code
