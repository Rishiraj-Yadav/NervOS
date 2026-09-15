"""C4 durable execution-retry policy: a pure, provider-neutral backoff decision.

The policy answers exactly one question — *when* may an already-classified safe execution
failure be claimed again — and nothing else. Eligibility, ownership, budget, persistence, and
reconciliation stay in the execution service and the durable transaction, so no policy change
can widen the replay surface on its own.

Nothing here performs I/O, reads a clock, sleeps, or knows a provider SDK. That is deliberate:
C4's only real danger is re-issuing a request that may already have reached a provider, so the
part that decides *when to replay* is a pure function over durable facts, and the only thing
that can decide *whether to replay* is the exact normalized failure classification plus the
live-claim and budget checks inside one `BEGIN IMMEDIATE` transaction.

The delay is capped and small by design. Rate limiting is the only failure C4 replays, the
whole retry horizon must stay far inside the frontend's bounded polling budget, and fairness
between Jobs is C6's concern rather than something an exponential schedule should approximate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

# 1s, 2s, 4s, then a flat 4s. No jitter: the schedule must be exactly reproducible from durable
# state, because an uncertain COMMIT has to be reconciled against one expected due time.
RETRY_BASE_DELAY = timedelta(seconds=1)
RETRY_MAX_DELAY = timedelta(seconds=4)

# A retry ordinal is bounded by the shared claim budget, which the schema caps at 10, so the
# exponent can never grow without bound even for a maximum-budget Job.
RETRY_MAX_ORDINAL = 10


class RetryPolicy(Protocol):
    """The delay decision the retry-scheduling transaction depends on."""

    def delay(self, ordinal: int) -> timedelta: ...


def retry_delay(ordinal: int) -> timedelta:
    """Return the capped exponential delay for a 1-based retry ordinal."""
    if ordinal < 1:
        raise ValueError("retry ordinal must be positive")
    exponent = min(ordinal, RETRY_MAX_ORDINAL) - 1
    return min(RETRY_BASE_DELAY * 2**exponent, RETRY_MAX_DELAY)


@dataclass(frozen=True, slots=True)
class CappedExponentialRetry:
    """The production policy: a small capped exponential delay with no randomness."""

    def delay(self, ordinal: int) -> timedelta:
        return retry_delay(ordinal)


PRODUCTION_RETRY_POLICY = CappedExponentialRetry()


def retry_due_at(policy: RetryPolicy, *, anchor_at: datetime, ordinal: int) -> datetime:
    """Return the absolute due instant for one logical retry-scheduling operation.

    `anchor_at` is captured once per logical outcome finalization and stays fixed across every
    safe database-only replay of that transition. The transaction's own clock is used
    separately for lease/fencing checks; it deliberately does not participate here, so a
    replayed transition computes the identical due time and an uncertain COMMIT can be
    reconciled against exactly one expected value.
    """
    if anchor_at.tzinfo is None:
        raise ValueError("retry anchor must be timezone-aware")
    return anchor_at + policy.delay(ordinal)
