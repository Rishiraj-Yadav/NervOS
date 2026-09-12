# NervOS Overview

## Status

This document describes the target product architecture. Only the Stage A subset exists today; read `docs/implementation-status.md` to see what is actually implemented.

## One-line definition

NervOS is a self-hosted runtime and management platform for installing, executing, scheduling, monitoring, and controlling AI agents on user-owned hardware.

## What NervOS is

NervOS provides shared infrastructure around agents:

- installation and configuration
- lifecycle management
- execution/runtime
- scheduling and event triggers
- conversation sessions
- scoped memory
- AI model routing
- tool/MCP access
- permissions and approvals
- secrets handling
- logging and artifacts
- updates/uninstallation
- dashboard
- developer SDK
- marketplace integration

## What NervOS is not

NervOS is not:

- an AI model
- a single chatbot
- a system that writes every agent itself
- a cloud service that must execute every user's agent
- a requirement to keep every installed agent process permanently running

## Actors

### NervOS project

Builds the platform: runtime, dashboard, SDK, package format, security boundaries, default agents/tools, and marketplace infrastructure.

### Agent developer

Uses the NervOS SDK and package format to implement one specific agent such as Research Assistant, Invoice Reminder, File Organizer, or Home Monitor.

### End user

Installs NervOS, installs an agent, grants permissions, configures their own accounts/preferences, and runs the resulting AgentInstance on their NervOS host.

## Core domain concepts

### AgentPackage

A versioned distributable software package created by an agent developer.

### AgentInstance

A user's installed/configured copy of an AgentPackage. Two users can install the same package while keeping separate configuration, secrets, schedules, sessions, and memory.

### Run

One execution of an AgentInstance. A run may be triggered by a user, schedule, webhook/event, device event, or future agent handoff.

### Session

A persistent human-agent conversation context. A session can contain many runs. Background scheduled runs may have no conversation session.

### Tool

A capability available through NervOS, normally mediated by the NervOS tool/permission layer and later by MCP where appropriate.

### Memory

Persistent information available to future runs according to an explicit scope.

## Target user flow

```text
Install NervOS
  -> first-run setup
  -> connect model/tool providers
  -> browse/install agent
  -> review permissions
  -> configure agent
  -> activate
  -> trigger/schedule creates a job
  -> NervOS executes the run
  -> inspect results, logs, artifacts, and memory
```

## Self-hosting boundary

The NervOS runtime, local state, sessions, permissions, logs, and local files can remain on the user's machine.

If the user selects a cloud AI provider, the prompt/context required for that model call leaves the machine and is processed by that provider. Self-hosting the runtime does not automatically make third-party inference local. A local model adapter can provide a fully local inference path.

## Version 1 philosophy

Build a reliable single-node product first. Do not make the first usable version depend on Kubernetes, peer-to-peer compute, distributed GPU sharing, self-modifying agents, large swarms, or a huge IoT ecosystem.
