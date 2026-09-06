# NervOS Runtime

## Status

Target architecture. The agent runtime is not part of Stage A.

## Purpose

The runtime executes one agent run inside a controlled environment while NervOS supplies common infrastructure and the agent package supplies domain-specific behavior.

## Trigger-to-result flow

```text
Trigger
  -> Create Job
  -> Persistent Queue
  -> Worker claims job
  -> Create/load Run
  -> Run Coordinator
       - load AgentInstance
       - load AgentPackage
       - load config
       - load permissions
       - load Session if interactive
       - retrieve relevant Memory
  -> RunContext
  -> Agent implementation
       - Model Router
       - Memory Service
       - Tool Gateway -> Permission Engine -> MCP/Tool
       - Artifact Service
  -> persist events/result/memory
  -> terminal Run state
```

## Agent state vs run state

Agent state describes whether an installed instance may be triggered:

```text
installing -> needs_configuration -> ready -> active
                                      |       |
                                      v       v
                                   disabled  paused
```

`active` does not mean the agent currently consumes CPU.

Run state describes one execution:

```text
queued -> claimed -> running
                    |   |   \
                    |   |    -> waiting_approval
                    |   -> waiting_external
                    -> retrying
                    -> succeeded / failed / cancelled
```

## Concurrency

Target defaults:

- many different agents may run concurrently subject to node resources
- one AgentInstance gets `max_concurrent_runs = 1` by default
- extra jobs wait in the queue
- agents may explicitly opt into safe higher concurrency later

## Worker model

Do not create a permanent process per installed agent. Workers process queued jobs; an AgentInstance with no job is normally persisted state only.

## Runtime limits

Later policies may include:

- timeout
- max model calls
- max tool calls
- CPU/RAM limit
- artifact quota
- retry count
- priority

## Run events

Keep an append-only execution trace such as:

- run.started
- context.loaded
- model.requested
- model.completed
- tool.requested
- tool.approved
- tool.completed
- artifact.created
- memory.updated
- run.completed
- run.failed

This event log powers debugging, auditability, realtime UI, and future metrics.
