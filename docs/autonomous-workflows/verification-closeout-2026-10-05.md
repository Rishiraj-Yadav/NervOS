# Autonomous workflow repair closeout — 2026-10-05

## Scope

This closes the owner-authorized W0–W6 repair plan and supersedes the reproduced
defects in the 2026-10-04 review. NervOS remains the lifecycle authority:
Scheduler continuations create ordinary Runs and Jobs; Workers commit bounded
JSON checkpoints inside the ordinary fenced success transaction.

## Repairs

- Continuation admission rolls back cleanly on refusal. Terminal linked Runs,
  cancellation, paused parents, unreleased waits and queue fairness are reconciled.
- Model output reservations account for every permitted call. Checkpoint retention,
  immutable state decoding, exact-revision signals and expiring one-use decisions work.
- Package proposals receive host-bound identity. LangGraph supports one application
  node per Run and rejects independent checkpointers, stores and multi-node topology.
- Gmail list/get use a bounded HTTPS transport behind encrypted account brokering,
  exact native definitions, explicit bindings/grants and the existing tool mediator.
- Research collects supplied tool evidence and calls the model for its brief. Mail
  parses immutable results, classifies actual messages and previews a review draft;
  approval resumes a read, and decline cancels. Neither package can send email.
- Dashboard creation, polling, signals, decision previews, private checkpoint
  inspection and actual Run navigation are covered. Native Gmail aliases are selectable.
- Unsupported package installations fail before environment construction. Blocking
  installation runs in the API thread pool. Production isolation is unchanged.

## Evidence

Disposable databases and deterministic external services were used throughout.
No developer database, real mailbox or provider credentials were used.

| Gate | Result |
|---|---|
| Full Python regression run | 3,506 passed, 6 skipped; one stale host-payload expectation failed |
| Corrected host and installation regression run | 28 passed; the stale expectation now includes `wake_signal` |
| Added installation preflight regression | 3 passed, including refusal before registry mutation |
| Final package API regression | 13 passed |
| Final workflow and generated SDK demo regression | 42 passed |
| Native account dispatcher security regression | 11 passed |
| Frontend | 176 passed; TypeScript, ESLint and production build passed |
| Python static checks | Ruff passed; Pyright zero errors |
| Security scan | 848 files scanned, no findings |
| Linux isolation qualification | 12/12 properties passed, including integrated runtime checks |
| Installed signed workflow packages on Linux | Research and mail restart/resumption journeys passed |
| Browser and runtime supervisor | Five journeys passed, with recovery, retry, cancellation and tool-ledger assertions |

The original full-suite failure is retained in its log, rather than described as
a clean full-suite pass. Focused reruns verified its correction and subsequent
changes. Migration upgrade/downgrade coverage exercised head `0024_durable_workflows`.

Logs are under `artifacts/workflow-*.txt`; browser results also appear in the
ignored Playwright result directory. Fixtures are not production credentials.

## Operational limits

This is a verified workflow MVP, not qualification against a live Google account.
Gmail OAuth configuration, account connection, exact readonly scope, egress origin
and per-agent grants remain deployment prerequisites; follow the runbook.
Windows refuses uncontained packages. Use supported Linux for package agents.
Built-in agents and workflow control surfaces work on Windows. No sending,
arbitrary framework compatibility, automatic uncertain-effect replay or permanent
agent process was added. Git publication is outside this task.
