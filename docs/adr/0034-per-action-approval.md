# ADR 0034 — Per-Action Approval: Durable Ask/Approve/Deny

Status: Accepted for Stage H implementation (H3)

## Context

Stage D froze two of the three permission branches — `allowed` and `denied` — and explicitly
excluded the third: *"The `approval required` branch is not implemented … That branch is Stage H"*
(docs/permissions.md, ADR 0015). Pausing a durable Run for a human decision must not create a
second execution authority, must not weaken the ADR 0017 no-replay rule, and must not let a
later-created grant or approval retroactively authorize an in-flight Run.

## Decision

### The approval state machine

```text
pending ──approve──► approved ──(one dispatch)──► consumed
   │                                                    │
   ├─deny────► denied ◄─────────────────────────────────┘ (dispatch re-check fails → denied)
   ├─expire──► expired
   ├─cancel──► cancelled   (owner withdraws a pending request)
   └─revoke──► revoked     (grant/connection/tool revoked while pending or approved-but-undispatched)
```

`denied`, `expired`, `cancelled`, `revoked` are terminal with **zero dispatches**. `approved` is
consummated by **exactly one** dispatch or reverted to a terminal state by a failed re-check;
an approved request can never authorize a second dispatch. `consumed` is terminal evidence.

### Binding and identity

Every approval row binds, immutably at creation: owner, AgentInstance, Run, Job, Attempt,
exact tool/connector identity (definition id + reviewed fingerprint + upstream name), the
canonical-schema version, and a **digest of the exact action input** (canonical JSON SHA-256,
same primitive the invocation audit already uses). A memory-suggestion approval (Stage F/0031)
is a different resource and never satisfies an external-action check, and vice versa.

A *fingerprint* of the reviewed action contract (schema + tool identity) is displayed to the
owner; the raw payload is shown only as a **safe preview** (argument shapes and redacted string
values, bounded), never credentials and never unbounded raw provider payloads.

### The gate in the mediator

The Stage-D `ToolInvocationMediator` gains one predicate between its existing permission check
and `record_requested`: if the descriptor/action class is approval-requiring, the live decision
must find a **matching, unexpired, approved, unconsumed** approval whose Run, Attempt, identity,
and input digest all equal the current call's. The gate is evaluated twice — once before
`requested` commits and again inside the `started` transaction (the existing re-check point),
so revocation, expiry, cancellation, or a consumed flag between the two commits fails closed
with zero dispatches. Actions that do not require approval take the unchanged Stage-D path.

### No retroactive authorization

- The approval must reference the **same Run** it authorizes: an approval created after a Run's
  snapshot (by Run id comparison) can never satisfy a different, earlier Run.
- Grant/connection/credential state is re-checked live at dispatch exactly as Stage D already
  does; an approval adds a necessary condition, never a sufficient one.
- A Run's tool-grant cutoff is unchanged; approvals ride the same Attempt fencing (`ClaimHandle`)
  as every other tool write, so a Worker restart, lease loss, or duplicate delivery cannot
  resurrect authority.

### Expiry and races

- Approvals carry a durable `expires_at` (default 15 minutes, bounded 1 min–24 h at creation).
  Expiry is evaluated from durable state at every check — no background sweeper is authoritative.
- Duplicate delivery (the same `request_id` replayed by a package host) resolves to the same
  approval row; a second dispatch attempt finds it consumed and is denied.
- The approve/deny API is idempotent per decision with `expected_state` CAS, so two dashboard
  tabs cannot double-consume.
- Cancellation of a Run denies all its pending approvals (the attempt authority is already gone).

### Honest limits (must appear in UI copy and docs)

- Approval authorizes NervOS to dispatch once; it **cannot recall** an action already sent to an
  external service.
- An approved-then-crashed dispatch is `ambiguous` exactly per ADR 0017 — the approval is marked
  consumed only on a recorded dispatch, and a replayed Attempt never auto-approves.

### Audit

`action_approvals` is durable evidence: requested/approved/denied/consumed/terminal instants,
decided-by, and the same digest family as invocations. Run Events gain one public event type
(`tool.approval_requested`, `tool.approval_decided`) carrying **no** arguments, digests beyond
the safe preview fields, or credentials — same writer-side guarantee as existing tool events.

## Consequences

- A human can stand between a model and a destructive action without pausing the whole engine:
  the Run simply fails safe (the tool denial is observed by the model as a safe error, matching
  Stage D's denial semantics) and the owner decides for the next Run.
- Approval adds latency and one more fail-closed predicate; that is the point.
- The durable ambiguity rules are untouched: no approval converts an ambiguous outcome into a
  known one, and no approval licenses replay.
