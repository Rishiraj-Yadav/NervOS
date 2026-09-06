# NervOS Permissions

## Status

Stage A implements normal dashboard authentication/authorization only. Agent capability permissions and approvals are future runtime features.

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

## Future permission flow

```text
Agent requests capability
  -> NervOS Tool API
  -> Permission Engine
       denied -> controlled denial
       approval required -> pause Run -> ask user -> approve/reject
       allowed -> Tool/MCP Gateway -> external action
```

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
