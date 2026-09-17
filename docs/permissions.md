# NervOS Permissions

## Status

Stage A implements normal dashboard authentication/authorization. Stage C is complete, so the durable
execution engine exists. **Stage D implements agent capability permissions**: persistent per-Agent
tool grants, held as explicit ALLOW rows and re-evaluated live before every tool call (ADR 0015).

**Approvals remain future.** The `waiting_approval` flow described below is **not** implemented in
Stage D; it belongs to Stage H, together with encrypted secrets and isolation.

## Principle

Grant the smallest capability needed. Avoid giant permissions such as "Gmail access".

Prefer granular capabilities:

```text
gmail.read
gmail.search
gmail.draft
gmail.send
gmail.delete

filesystem.read
filesystem.write
filesystem.delete

calendar.read
calendar.create
calendar.delete

database.read
database.write

iot.sensor.read
iot.actuator.control
```

## Permission flow

```text
Agent requests capability
  -> NervOS Tool API
  -> Permission Engine
       denied -> controlled denial
       approval required -> pause Run -> ask user -> approve/reject
       allowed -> Tool/MCP Gateway -> external action
```

**Stage D implements the `denied` and `allowed` branches.** The decision is evaluated live from the
durable grant rows immediately before each tool call, so a revocation takes effect before the next
call; an already-dispatched call cannot be recalled.

**The `approval required` branch is not implemented.** Stage D has no way to pause a Run and ask a
user, so nothing may render a control that implies it does. That branch is Stage H.

Agent code must not bypass this path.

## Risk classes

Suggested categories:

- Low: public/read-only information such as weather
- Medium: private read access such as email/files/calendar
- High: external side effects such as send email/write file
- Critical/explicit approval: delete files, spend money, unlock physical device, public publishing, destructive DB action

Risk classification is metadata; actual user grants remain explicit.

## Approval state

The future Run state machine should support `waiting_approval` with audit information: exact action, agent, target, parameter summary, expiration, approval scope, result.

## Tool connection != agent permission

A user may have a Gmail OAuth connection without giving every agent Gmail access. Tool connections and AgentInstance capability grants are separate records/concepts.

## Secrets

Permission to use a capability must not imply access to the raw credential. NervOS holds secrets and performs/proxies the authorized operation.
