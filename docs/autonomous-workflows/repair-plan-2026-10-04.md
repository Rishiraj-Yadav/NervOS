# W0–W6 integration repair plan

Authorized by the owner on 2026-10-04. Completed and verified on 2026-10-05;
see [verification closeout](verification-closeout-2026-10-05.md) for evidence and limits.

1. Protect continuation insertion with a savepoint so identity refusal cannot
   leave a Run, Job, events or context behind. Reconcile terminal linked Runs
   without replay. Complete legacy successful steps inside the success fence.
2. Exclude unreleased waits and live Runs from dispatch scans, rotate deferred
   work durably, and reserve all permitted model output across calls. Enforce
   the ADR 0039 LangGraph contract, including refusal of native checkpointers.
3. Compose bounded Gmail read transport behind account brokering and the existing
   tool mediator. Keep tokens private and sending/deletion unavailable.
4. Replace fabricated research evidence and incorrect immutable mail parsing.
   Test generated agent code and qualify installed packages on supported Linux.
5. Complete workflow creation, live status, signals, decision previews and valid
   Run navigation. Cover operator behavior in unit and browser tests.
6. Run isolated migration, Python/frontend regression, static/security and Linux
   runtime gates. Correct documentation only on demonstrated acceptance.

No second queue, automatic replay of uncertain effects, Stage-J implementation,
weakened sandbox, live developer database access or Git publication is included.

Completion requires working production paths and acceptance evidence, not only
passing persistence fixtures. Any unavailable qualification must remain explicit.
