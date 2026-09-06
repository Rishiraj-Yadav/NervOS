---
name: add-agent
description: Add a built-in or example NervOS agent using the public Agent SDK/package contract without bypassing runtime, model, memory, tool, or permission abstractions. Intended for Stage B and later.
---

# Add a NervOS Agent

Do not use during Stage A.

Read `docs/overview.md`, `docs/architecture.md`, `docs/runtime.md`, `docs/memory.md`, `docs/permissions.md`, and `docs/agent-packages.md`.

## Define before coding

- package id/name/version
- user problem and expected output
- trigger types
- model capabilities required
- tools/capabilities required
- permissions requested
- configuration schema
- memory scope needs
- artifact/output behavior
- timeout/concurrency expectations

## Rules

- use public SDK abstractions
- do not access internal ORM models from agent code
- do not receive raw credentials
- do not bypass tool permission gateway
- keep user-specific config outside package source
- do not automatically read unrelated agent memory
- deterministic logic should stay deterministic instead of forcing LLM calls

## Completion

Add implementation, manifest, config schema, tests, README/example, and install/run smoke test once package management exists.
