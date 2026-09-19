# ADR 0020 — Run Provenance and the Shared Acceptance Seam

Status: Accepted for E0 architecture freeze

This ADR freezes **how a Run comes into existence**, and how a Run answers *why it exists*. The
scheduling semantics that decide when are ADR 0018; the ingress that can cause one is ADR 0019.

## Context

Every Run in NervOS so far was created the same way, by the same code: an authenticated browser request
reaches a thin route, the route delegates to a provider-neutral application service, and that service
drives a durable persistence primitive that commits a Run, a Job and the two opening Run Events in one
serialized transaction.

Stage E is the first milestone that needs a **second caller** of that path. A scheduler and two
ingresses will need to create Runs too. The risk is not that this is hard; it is that it is easy to do
badly. The three attractive mistakes are:

1. **Duplicate the insertion.** Let the scheduler write its own `runs` and `jobs` rows, because it
   already has an open transaction and calling the existing primitive would be awkward.
2. **Leak the transaction.** Pass a database connection up into an application-layer protocol so the
   scheduler can join the transaction.
3. **Rebuild the most safety-critical table in the system** to add a provenance column, on the way to a
   milestone that is supposed to add no execution semantics at all.

All three are avoidable, and this ADR records why.

Two source facts make the correct design small rather than ambitious:

**The seam already exists and is already provider-neutral.** `AgentService.submit_run` in
`packages/nervos-core/src/nervos_core/application/agents.py` imports no FastAPI and no HTTP type, and is
already invoked outside the API plane. Manual Run creation does **not** live partly inside a route; the
route is a single delegation.

**The durable primitive already takes a caller-owned connection.** The inner operation that performs the
Run insert, the Job insert and the two events is already connection-taking — it is simply private. What
Stage E needs is not a new implementation but a promotion.

## Decision

### One canonical Run + Job insertion implementation

There is exactly **one** implementation that inserts a Run, its Job, and the opening
`run.created` / `run.queued` events. Both manual submission and trigger materialization call it. Its SQL
is never duplicated.

It continues to guarantee, unchanged:

- ownership and enabled-state re-checked **inside** the transaction, so a race a pre-flight read cannot
  close is caught at commit time;
- the Agent snapshot taken from the Agent Instance and the resolved definition **at that instant**;
- the **grant cutoff read inside the same transaction**, so a capability granted a moment later is
  ordered after this Run and can never be acquired by it;
- Run, Job and both events committed atomically, or not at all.

### The connection boundary sits in infrastructure, not in an application protocol

**No SQLAlchemy connection type appears in a domain type, an application protocol signature, or any
provider-neutral Stage E port.**

An application protocol that mentions a database connection is not provider-neutral — it has made the
persistence technology part of its interface, and every future implementation, test double and
alternative backend inherits that. The repository already forbids SQLAlchemy in `domain/`; this ADR
extends the same reasoning to the Stage E application ports.

The split:

| Layer | Owns |
|---|---|
| **Application** | resolving and validating the request using provider-neutral values; calling a provider-neutral persistence operation; translating its outcome into domain results |
| **Infrastructure** | opening the transaction; inserting and finalizing the occurrence; invoking the shared connection-taking Run insertion helper; linking the occurrence to the Run; advancing the schedule state; committing atomically |

The **connection-bound helper is infrastructure-private**. It is reachable from another infrastructure
module, never from application or domain code.

### The atomic materialization seam

Stage E's persistence port takes a **command of provider-neutral values** and returns a domain value:

```
materialize_occurrence_and_run(command: TriggerMaterializationCommand) -> TriggerOccurrence
```

The command carries only what the application layer knows: the trigger identity, the occurrence
identity for the kind, the observed revision, the composed input text, the owner and Agent identity,
the payload digest and size, and the instant. It carries **no connection, no session, no engine, no
rows**.

The infrastructure implementation owns, in **one** `BEGIN IMMEDIATE` transaction:

1. re-read the trigger and its authority (enabled, owner, target, secret where applicable);
2. insert the occurrence;
3. invoke the one canonical Run + Job insertion;
4. link `occurrence.run_id` to the new Run;
5. advance the trigger's scheduling state, when a later milestone calls it;
6. commit.

Everything, or nothing. A crash at any point before commit leaves the trigger still due, because the
transaction rolled back and there is no partial durable state to repair. A crash after commit leaves an
ordinary Run that Stage C recovery, retry, cancellation and concurrency already own.

**This is why the seam exists at all.** A design in which the occurrence is inserted by one transaction
and the Run by another has a window — after the occurrence, before the Run — that can only be repaired
by guessing whether the Run was created. That window is the whole reason the architecture insists on a
single transaction, and it is why the connection-taking helper must be shared rather than reimplemented.

The **no-network and no-sleep rule** applies to this transaction: the ingress has already received
anything remote, and the scheduler sleeps outside it.

### Provenance is a reverse relation, not a column on `runs`

**Frozen:**

```
trigger_occurrences.run_id     UNIQUE     FK runs.id     ON DELETE RESTRICT
```

This is the entire provenance mechanism.

- A **manual** Run has no occurrence pointing at it.
- A **triggered** Run has exactly one occurrence pointing at it.

Origin is therefore **derived, never stored**:

```
no matching occurrence  → manual
matching occurrence     → the trigger's kind, from the TriggerDefinition
```

**`runs` gains no column. `Run` gains no provenance field. The submission path gains no provenance
parameter.**

The reasoning is a cost argument with a safety consequence:

- Every change to `runs` in this repository is a **full table rebuild**, not an added column. Two prior
  migrations rebuild it, and the rebuild re-emits the entire table definition — including the large
  per-status lifecycle `CHECK`, the input bounds, the tool-limit bounds and the finish-order constraint
  — for the table every other invariant in the system depends on.
- The reverse relation needs **no `runs` migration at all**.
- Reading provenance costs one indexed lookup, and the Run read path **already joins another table** to
  derive execution phase, so a join is the shape the read path already uses.
- A provenance parameter on the submission path would exist solely to carry an identifier the caller
  **already holds** — the materialization transaction owns the occurrence, so it can link the two rows
  itself with no help from the application layer.

**What this buys and what it costs.** It buys a milestone that adds no column to the safety-critical
table and no widening of the Run domain value. It costs one join on a path that already joins. If a
future UI needs provenance on a Run list, the join is already there.

### Manual submission is semantically unchanged

The refactor is a promotion, not a redesign. What must remain true after it, and is asserted:

- the manual endpoint's observable behaviour is unchanged — same status, same `Location` header, same
  response shape, same error mapping;
- **one** transaction, in the same order, with the same validation sequence;
- the same events, with the same contiguous sequence;
- the same snapshot columns;
- the same grant cutoff read, inside the same transaction;
- the same ownership and enabled checks, inside the same transaction.

Anything that would change the manual path's durable output is a regression, not a refactor.

### The guards get stronger, not weaker

An existing architecture guard asserts that exactly one canonical acceptance path exists. Stage E adds
callers of one implementation, not a second implementation — so the guard should be **strengthened**
into the stronger, more honest invariant: **one insertion implementation, with a named and reviewed set
of authorized callers**, rather than a count of a string in route modules.

Weakening that guard would be the single most damaging thing Stage E could do to the property it is
supposed to preserve.

### Stage E acquires no execution semantics

The scheduler and the ingresses create Runs. They do not claim, start, renew, inspect, terminalize,
retry, recover or cancel anything. The Scheduler process holds **no provider credential** and exposes
**no HTTP surface**.

Consequently every property Stages C and D built applies to a triggered Run automatically, because a
triggered Run *is* an ordinary Run:

the same Job, the same `UniqueConstraint("run_id")` one-Job-per-Run shape, the same queue and claim
semantics, the same lease fencing, the same retry policy and its two-layer dispatch guard, the same
cancellation authority, the same concurrency and fairness limits, the same provider limits, the same
tool loop, the same live permission evaluation, the same grant cutoff, the same `ToolInvocation`
ledger, the same ambiguity handling, the same Run Events.

**A Run created automatically is not more privileged than a Run created by hand.** This is the property
Stage E exists to preserve, and it is preserved by not adding a path rather than by adding a check.

### No exactly-once claim about external effects

**Preserved unchanged from ADR 0017.**

A triggered Run that performs a tool side effect can still become **ambiguous**: the effect may have
happened and the outcome may be unknown. There is no exactly-once guarantee for external effects, and
no automatic whole-Run replay after dispatch.

Autonomy raises the stakes rather than lowering them — an unattended Run may perform an irreversible
external action and then lose its Worker. Stage E does not mitigate that; it inherits the honest
behaviour: the invocation closes ambiguous, the Run fails, and nothing is replayed. Approval and
sandboxing are Stage H's.

The guarantee Stage E *does* make is narrower and truthful: **one deterministic schedule occurrence
produces at most one NervOS Run.** That is a statement about local durable state, not about a remote
system's behaviour.

### Provenance reads never become authority

The provenance relation answers "why does this Run exist?" It is **not** consultable by anything that
decides execution. No module that claims, starts, retries, recovers or terminalizes may read it, for
the same reason no such module may read Run Events to decide anything: a descriptive record that can
influence execution is not descriptive.

## Consequences

**The milestone adds no column to the table everything depends on.** The most safety-critical schema in
the system is untouched, and provenance — a genuinely new fact — is recorded without a rebuild, a
constraint re-emission, or a widened domain type.

**The application layer stays honest about its boundaries.** A future alternative persistence
implementation, or a test double, can satisfy the Stage E ports without knowing what a connection is.

**One implementation means one place to be right.** Every C/D guarantee applies to every Run because
there is one code path that creates one, and the guard that protects that property is stronger than it
was before rather than weaker.

**The cost is that materialization is infrastructure-heavy.** The transaction, the authority re-read,
the occurrence write, the Run creation, the link and the schedule advance all live in one infrastructure
method. That is more logic in infrastructure than the repository's other ports carry — and it is the
correct place for it, because every one of those steps is a database operation that must be atomic with
the others.

**Provenance needs a join rather than a column read.** Accepted: the read path already joins, and the
alternative was a rebuild of the runs table.

**A triggered Run is still capable of an irreversible external effect that then becomes ambiguous.**
No architecture in Stage E changes that; ADR 0017 owns the honest handling, and Stage H owns the
approval boundary that would prevent it.
