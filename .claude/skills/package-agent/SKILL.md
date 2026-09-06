---
name: package-agent
description: Build, validate, verify, install-test, or publish a `.nervos` agent package according to manifest, compatibility, permissions, configuration, hashing, and signing rules. Intended for Stage G and later.
disable-model-invocation: true
---

# Package a NervOS Agent

Read `docs/agent-packages.md` and security rules.

## Before packaging

Verify:

- tests pass
- package id/version are correct
- manifest validates
- entrypoint exists
- configuration schema validates
- permissions match actual behavior
- no real secrets are included
- no developer-local absolute paths are included
- dependencies are reproducible/locked where supported

## Produce

Target artifact: `<agent-name>-<version>.nervos`.

Generate package hash/signature once signing exists.

## Validate

Perform a clean local install through the same package installer used by marketplace installs.

Never publish automatically unless the user explicitly requests publication.
