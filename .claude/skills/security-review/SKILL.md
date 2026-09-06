---
name: security-review
description: Review NervOS changes for authentication, authorization, secrets, injection, path/file safety, permissions, agent isolation boundaries, sensitive logging, and unsafe defaults.
---

# NervOS Security Review

Read `.claude/rules/security.md` and `docs/permissions.md`.

Inspect actual files; do not speculate.

## Review categories

- authentication bypass
- authorization/ownership failures
- plaintext credentials/tokens
- sensitive logging
- insecure cookies/session lifecycle
- SQL injection
- command injection
- path traversal
- unsafe filesystem operations
- overly broad CORS
- exposed stack traces
- raw credentials reaching agent/client code
- future runtime/tool bypasses around permissions

## Output

Classify findings as CRITICAL, HIGH, MEDIUM, or LOW.

For each finding provide:

- file/location
- vulnerability
- realistic consequence
- minimal recommended fix

If code modification was explicitly requested, fix confirmed issues and add regression tests. Otherwise review only.
