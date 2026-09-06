# NervOS Agent Packages

## Status

Target architecture. Package installation is a later stage.

## Purpose

An AgentPackage is the distributable software artifact produced by an agent developer and installed into a user's NervOS node.

YAML is metadata/configuration. It is not a replacement for complex agent logic.

## Recommended future package shape

```text
invoice-reminder.nervos
  manifest.yaml
  agent.whl
  config.schema.json
  README.md
  assets/
  migrations/
  signature.json
```

## Manifest responsibilities

Describe:

- package identifier
- name/publisher/version
- runtime/entrypoint
- minimum compatible NervOS version
- model capabilities
- tool requirements
- requested permissions
- supported triggers
- configuration schema
- memory declarations
- resource limits

Illustrative example:

```yaml
id: com.example.invoice-reminder
name: Invoice Reminder
version: 1.0.0
runtime:
  language: python
  entrypoint: invoice_reminder:InvoiceReminderAgent
nervos:
  min_version: "1.0"
models:
  capabilities: [text-generation]
tools:
  required: [invoices.read, telegram.send]
permissions: [invoices.read, telegram.send]
memory:
  private: true
configuration:
  schema: config.schema.json
```

## Package vs instance

The same package can create multiple independent instances:

```text
Raj's Invoice Reminder
  schedule = 09:00
  tool connections = Raj's
  memory = Raj's

Priya's Invoice Reminder
  schedule = Monday 10:00
  tool connections = Priya's
  memory = Priya's
```

## Target installation flow

```text
select/download package
  -> verify hash/signature
  -> parse manifest
  -> compatibility check
  -> show permissions
  -> user approval
  -> create isolated install environment
  -> install dependencies
  -> create AgentInstance
  -> collect config
  -> connect required tools/models
  -> create memory namespace
  -> register triggers
  -> health check
  -> ready/active
```

## Updates

Updates must preserve user-owned config/memory where compatible, show new permissions, run migrations safely, health-check before activation, and support rollback where practical.

## Uninstall

Distinguish package code from AgentInstance config, memory, artifacts, credentials, tool connections, and shared data. Never unexpectedly delete user data.

## Security

Marketplace packages are untrusted. Production third-party execution eventually requires containment/sandboxing and explicit tool access rather than unrestricted host access.
