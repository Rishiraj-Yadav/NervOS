# NervOS Permissions

## Status

Stage A implements normal dashboard authentication/authorization. Stage C is complete, so the durable
execution engine exists. **Stage D implements agent capability permissions**: persistent per-Agent
tool grants, held as explicit ALLOW rows and re-evaluated live before every tool call (ADR 0015).

**Approvals are implemented in Stage H.** The `approval required` branch described below is
delivered by Stage H (ADR 0034): a durable `action_approvals` row gates one exact action, enforced
inside the Stage-D mediator at both admission and dispatch time.

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

**Stage H implements the `approval required` branch** (ADR 0034). An MCP action that requires
approval is not dispatched: a durable `action_approvals` row is written and the loop pauses until a
human resolves it. The gate runs *after* the grant decision, so a call the grant already denied
never produces an approval request, and it re-checks by compare-and-set immediately before
dispatch, so an approval is consumed exactly once. Every refusal — deny, expiry, cancellation, or
revocation — fails closed with zero dispatches. See
[Stage H](stage-h/README.md) for the full state machine.

Agent code must not bypass this path.

## Risk classes

Suggested categories:

- Low: public/read-only information such as weather
- Medium: private read access such as email/files/calendar
- High: external side effects such as send email/write file
- Critical/explicit approval: delete files, spend money, unlock physical device, public publishing, destructive DB action

Risk classification is metadata; actual user grants remain explicit.

## Approval state

Stage H implements the approval gate with a **durable `action_approvals` row** rather than a
`waiting_approval` Run status. Each row records the exact action — agent, tool, redacted argument
preview, a digest binding the request, the Run/Attempt it belongs to, the resolved owner, an expiry,
and the scope (`once`) — plus who approved it and when. The approval is consumed by a
compare-and-set at dispatch, so one approval can authorize exactly one dispatch and cannot be
replayed by a retry or a Worker restart. See [ADR 0034](adr/0034-per-action-approval.md).

## Secrets

Permission to use a capability must not imply access to the raw credential. NervOS holds secrets and performs/proxies the authorized operation.

Stage H delivers this (ADRs 0032–0033). Secret values are stored AES-256-GCM encrypted in the
Secret Manager with the key held outside the database, are **write-only** through the API (a value
is never returned by any endpoint), and are resolved server-side by the credential broker at
dispatch time only. Neither package code, a model prompt, nor the browser ever receives a raw
value.

## Tool connection != agent permission

A user may have a Gmail OAuth connection without giving every agent Gmail access. Tool connections and AgentInstance capability grants are separate records/concepts.
