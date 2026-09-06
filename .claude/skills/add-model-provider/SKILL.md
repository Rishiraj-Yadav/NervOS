---
name: add-model-provider
description: Add a NervOS AI model provider adapter behind the model-provider interface, including capability metadata, configuration, error normalization, streaming/tool-call handling where needed, and tests. Intended for Stage B and later.
---

# Add a NervOS Model Provider

Do not implement providers during Stage A.

## Goal

Agents depend on a stable NervOS model interface, never directly on provider SDK objects.

## Define

- provider id
- model ids
- capability metadata
- authentication/configuration requirements
- request mapping
- response normalization
- streaming behavior
- tool-call support
- timeout/rate-limit/error normalization
- usage metadata when available

## Security/privacy

- never log API keys
- never store provider credentials in agent memory
- make local-vs-cloud data boundary explicit
- default tests use mocks/fakes, not paid external calls

Add adapter contract tests proving common interface behavior.
