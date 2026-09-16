"""Owner-authorized Run cancellation.

Cancellation is **authoritative and immediate**. The durable transition commits inside the call
that requests it, so it completes when no Worker is running, when the Worker has crashed, and at
every nonterminal Job state — queued, claimed before execution, actively running, or waiting on a
scheduled retry. None of that a request-then-acknowledge model could promise. The owning Worker
is a follower: it discovers the revoked authority on its next heartbeat and stops waiting on the
provider.

What this module deliberately does **not** claim: that the remote provider stopped processing a
request it already received. Cancelling the local wait is not remote cancellation, and NervOS
may not pretend otherwise. The durable promise is narrower and honest — no further NervOS
execution, no future claim or retry, and no late result can ever be persisted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from nervos_core.application.agents import RunNotFound
from nervos_core.application.clock import Clock, require_utc
from nervos_core.application.job_execution import CancellationOutcome
from nervos_core.domain.runs import Run


class RunNotCancellable(RuntimeError):
    """Raised when a Run already reached a terminal lifecycle cancellation may not rewrite.

    A succeeded or failed Run is history. Cancellation never converts one into a `cancelled`
    Run, because that would erase an outcome that really happened.
    """


class RunCancellationPersistence(Protocol):
    def cancel_run(self, *, user_id: int, run_id: int, now: datetime) -> CancellationOutcome: ...


class OwnedRunReader(Protocol):
    def get_run(self, owner_user_id: int, run_id: int) -> Run: ...


class RunCancellationService:
    """Cancel one owned Run durably, then report the resulting Run truthfully.

    The read happens after the write commits, so the returned Run is the durable result rather
    than a prediction of it. A cancelled Run is terminal and immutable, so the follow-up read
    cannot observe a later transition.
    """

    def __init__(
        self,
        cancellation: RunCancellationPersistence,
        runs: OwnedRunReader,
        clock: Clock,
    ) -> None:
        self._cancellation = cancellation
        self._runs = runs
        self._clock = clock

    def cancel_run(self, owner_user_id: int, run_id: int) -> Run:
        """Cancel one owned Run, or raise for a foreign, missing, or terminal Run.

        Foreign and nonexistent are the same `RunNotFound`, exactly as they are for reads, so
        cancellation can never be used to discover whether another user's Run exists.
        """
        outcome = self._cancellation.cancel_run(
            user_id=owner_user_id,
            run_id=run_id,
            now=require_utc(self._clock()),
        )
        if outcome is CancellationOutcome.NOT_FOUND:
            raise RunNotFound
        if outcome is CancellationOutcome.NOT_CANCELLABLE:
            raise RunNotCancellable
        return self._runs.get_run(owner_user_id, run_id)
