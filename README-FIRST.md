# NervOS Docs + Skills + Hooks Bootstrap

Copy the contents of this bundle into the root of the NervOS repository before giving Claude Code the Stage A implementation prompts.

## What is included

- all requested `docs/*.md` design documents
- five initial ADRs
- all 11 requested Claude Code skills
- hook documentation
- four cross-platform Python hook scripts
- `.claude/settings.example.json` showing how to enable the hooks and conservative permissions

## What you should do

1. Copy `docs/` into your repository.
2. Copy `.claude/skills/` into your existing `.claude/skills/`.
3. Copy `.claude/hooks/` into your existing `.claude/hooks/`.
4. Merge `.claude/settings.example.json` into your existing `.claude/settings.json` rather than overwriting custom settings blindly.
5. Keep the `.claude/CLAUDE.md`, rules, and reviewer agents from the earlier setup.
6. Start Stage A with planning prompt A0.

## Important

These docs intentionally describe future NervOS architecture before it is implemented. `docs/implementation-status.md` is the source of truth for what exists right now.

Claude should maintain/update these docs later, but it should not invent the initial architecture from scratch.
