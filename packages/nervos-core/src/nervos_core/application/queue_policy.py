"""C6 queue concurrency policy: the one authoritative source for every claimer.

Execution concurrency is *system policy*, not per-process tuning. It lives here, in the shared
core library, for the same reason `LEASE_DURATION`, `HEARTBEAT_INTERVAL`, and the C4 retry policy
live here: every Worker imports this module, so no two Workers can hold contradicting limits.

That property is the whole point, and it is why these are not environment variables. Each claim
compares a *database-wide* count against its limit, so a Worker whose limit were larger than
another's would let the fleet run above the intended bound -- divergent configuration would raise
the effective cap silently rather than being detected. A constant cannot diverge.

Admission backpressure is a different concept with a different reader: only the API admits work,
so its pending limits stay operator-facing settings. See `Settings.max_pending_jobs`.

These limits are ceilings, not reservations. Nothing here reserves capacity for a partition, and
nothing here is stored on a Run or a Job: concurrency is decided at claim time from live state.
"""

from __future__ import annotations

from dataclasses import dataclass

# Bounds for every active-concurrency limit. The upper bound is generous for a self-hosted,
# single-node SQLite deployment; the lower bound keeps a limit meaningful rather than a silent
# "never claim anything".
MIN_ACTIVE_LIMIT = 1
MAX_ACTIVE_LIMIT = 64

# How many Jobs may hold a live lease across all Workers at once. The default reproduces the
# effective node-wide concurrency the C2 Worker default already produced, so C6 changes no
# out-of-the-box behavior.
GLOBAL_ACTIVE_LIMIT = 4

# How many Jobs one Agent Instance may execute concurrently. Bounded independently of the global
# limit so one Agent cannot occupy the whole execution budget.
PER_AGENT_ACTIVE_LIMIT = 4

# How many Jobs one model provider may execute concurrently, applied to each distinct provider
# identifier independently. Provider-neutral: no provider is named anywhere in C6.
PER_PROVIDER_ACTIVE_LIMIT = 4


@dataclass(frozen=True, slots=True)
class QueuePolicy:
    """The authoritative execution-concurrency limits a claim must satisfy."""

    global_active_limit: int = GLOBAL_ACTIVE_LIMIT
    per_agent_active_limit: int = PER_AGENT_ACTIVE_LIMIT
    per_provider_active_limit: int = PER_PROVIDER_ACTIVE_LIMIT

    def __post_init__(self) -> None:
        for name in ("global_active_limit", "per_agent_active_limit", "per_provider_active_limit"):
            value = getattr(self, name)
            if not MIN_ACTIVE_LIMIT <= value <= MAX_ACTIVE_LIMIT:
                raise ValueError(
                    f"{name} must be between {MIN_ACTIVE_LIMIT} and {MAX_ACTIVE_LIMIT}"
                )


PRODUCTION_QUEUE_POLICY = QueuePolicy()


__all__ = [
    "GLOBAL_ACTIVE_LIMIT",
    "MAX_ACTIVE_LIMIT",
    "MIN_ACTIVE_LIMIT",
    "PER_AGENT_ACTIVE_LIMIT",
    "PER_PROVIDER_ACTIVE_LIMIT",
    "PRODUCTION_QUEUE_POLICY",
    "QueuePolicy",
]
