"""Test-only deterministic Worker process for the supervised offline browser journey.

This is the *shipped* Worker loop — the same `Worker`, `JobExecutionService`, `RunExecutor`,
claim, lease, heartbeat, and terminalization code production runs — composed with deterministic
offline completions instead of `nervos_models`. No cloud request is made and no credential is
required. It is launched as a plain script so the supervisor can point it at a temporary
database and a readiness marker outside the repository.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from deterministic import RETRY_DELAY_VARIABLE, FixedDelayRetry, build_deterministic_completions
from nervos_core.application.job_execution import ClaimedAttempt, JobExecutionService
from nervos_core.application.lease_reclamation import LeaseReclaimer
from nervos_core.application.retry_policy import PRODUCTION_RETRY_POLICY, RetryPolicy
from nervos_core.application.run_execution import RunExecutor
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobExecutionPersistence

# The Worker application itself is production code; only the provider mapping is a double.
from nervos_worker.app import require_schema_revision, write_ready_marker
from nervos_worker.config import WorkerSettings
from nervos_worker.identity import generate_worker_id
from nervos_worker.registry import ReclaimLoop, WorkerRegistry
from nervos_worker.service import Worker
from sqlalchemy.engine import Engine

logger = logging.getLogger("e2e_worker")

# Test-only claim gate. The supervisor points this at a path inside its own temporary
# directory and never creates it; the browser journey creates it once it has observed the
# queued Run. Only this script reads it — no production module and no production setting
# knows it exists, so the shipped Worker always claims immediately.
CLAIM_GATE_VARIABLE = "NERVOS_E2E_CLAIM_GATE"

# Test-only pre-start loss mode. When set, this process claims exactly one Job with the shipped
# `claim_next` and then exits *before* the execution-start boundary may commit, which is exactly
# the durable state a real pre-start Worker loss leaves behind. The supervisor then starts a
# second Worker, whose startup reclamation pass must reconcile that expired claim.
PRE_START_CRASH_VARIABLE = "NERVOS_E2E_PRE_START_CRASH"

# Test-only lease length for that single claim, so the supervisor can expire it deterministically
# instead of waiting for the production 60-second lease. Production never sets it.
CLAIM_LEASE_SECONDS_VARIABLE = "NERVOS_E2E_CLAIM_LEASE_SECONDS"
DEFAULT_CRASH_LEASE_SECONDS = 1


class GatedJobPersistence(SqlAlchemyJobExecutionPersistence):
    """Refuse to claim while the gate file is absent so the queued state is observable.

    Without this the journey could pass by luck: the browser might render the accepted Run
    before a Worker claimed it, or might never render it at all, and the test could not tell
    the asynchronous cutover apart from the superseded synchronous behaviour. Gating the
    claim makes "the Run is visible as queued" a deterministic precondition of execution
    rather than a race the test hopes to win.
    """

    def __init__(self, engine: Engine, gate_path: Path) -> None:
        super().__init__(engine)
        self._gate_path = gate_path

    def claim_next(
        self,
        *,
        worker_id: str,
        provider_ids: Sequence[str],
        max_active: int,
        now: datetime,
        lease_duration: timedelta,
    ) -> ClaimedAttempt | None:
        """Claim normally, but only once the browser has released the gate."""
        if not self._gate_path.exists():
            return None
        return super().claim_next(
            worker_id=worker_id,
            provider_ids=provider_ids,
            max_active=max_active,
            now=now,
            lease_duration=lease_duration,
        )


def utc_now() -> datetime:
    return datetime.now(UTC)


def resolve_retry_policy() -> RetryPolicy:
    """Return the retry policy this Worker runs with.

    Production composition injects the reviewed schedule and never reads this variable, so the
    supervised journey can stretch the durable wait long enough to observe it without changing
    any shipped constant.
    """
    override = os.environ.get(RETRY_DELAY_VARIABLE, "").strip()
    if not override:
        return PRODUCTION_RETRY_POLICY
    return FixedDelayRetry(float(override))


def install_stop_handlers(
    loop: asyncio.AbstractEventLoop, stop: asyncio.Event
) -> list[signal.Signals]:
    def request_stop(_signum: int, _frame: object) -> None:
        loop.call_soon_threadsafe(stop.set)

    installed: list[signal.Signals] = []
    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        with contextlib.suppress(ValueError, OSError, RuntimeError, NotImplementedError):
            signal.signal(number, request_stop)
            installed.append(number)
    return installed


async def claim_once_then_die(
    *,
    engine: Engine,
    persistence: SqlAlchemyJobExecutionPersistence,
    settings: WorkerSettings,
    revision: str,
) -> int:
    """Claim exactly one Job and exit before the execution-start boundary.

    The claim transaction is the shipped `claim_next`, so the Job and its Attempt are `claimed`
    with a real 32-byte token and a real lease — the precise durable state a Worker that died
    before starting leaves. This path never constructs a provider client, never calls
    `start_attempt`, and never executes anything.

    A short, test-only lease is used so the supervisor can expire it deterministically rather
    than waiting out the production 60-second window.
    """
    completions = build_deterministic_completions()
    provider_ids = tuple(sorted(completions))
    granted = timedelta(
        seconds=int(
            os.environ.get(CLAIM_LEASE_SECONDS_VARIABLE, DEFAULT_CRASH_LEASE_SECONDS)
            or DEFAULT_CRASH_LEASE_SECONDS
        )
    )
    worker_id = generate_worker_id()
    registry = WorkerRegistry(persistence, worker_id, clock=utc_now)
    reclaimer = ReclaimLoop(LeaseReclaimer(persistence), clock=utc_now)
    registry.register()
    reclaimer.startup_pass()

    marker = settings.require_worker_ready_file()
    if marker is not None:
        write_ready_marker(marker, revision=revision, provider_ids=provider_ids)

    gate = os.environ.get(CLAIM_GATE_VARIABLE, "").strip()
    if gate:
        # Wait for the browser to release the gate, so the queued Run is observable first.
        while not Path(gate).exists():
            await asyncio.sleep(0.05)

    claimed = await asyncio.to_thread(
        persistence.claim_next,
        worker_id=worker_id,
        provider_ids=provider_ids,
        max_active=settings.max_active_jobs,
        now=utc_now(),
        lease_duration=granted,
    )
    if claimed is None:
        raise RuntimeError("pre-start crash worker found no eligible Job to claim")
    crash_marker = Path(os.environ[PRE_START_CRASH_VARIABLE])
    crash_marker.write_text(
        f"run_id={claimed.run_id}\njob_id={claimed.job_id}\nattempt_id={claimed.attempt_id}\n",
        encoding="utf-8",
    )
    logger.info(
        "e2e_worker_crashed_before_start worker_id=%s run_id=%s job_id=%s attempt_id=%s",
        worker_id,
        claimed.run_id,
        claimed.job_id,
        claimed.attempt_id,
    )
    return 0


async def run() -> int:
    settings = WorkerSettings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,
    )
    engine = create_sqlite_engine(settings.database_path)
    try:
        revision = require_schema_revision(engine)
        gate = os.environ.get(CLAIM_GATE_VARIABLE, "").strip()
        persistence = (
            GatedJobPersistence(engine, Path(gate))
            if gate
            else SqlAlchemyJobExecutionPersistence(engine)
        )
        if os.environ.get(PRE_START_CRASH_VARIABLE, "").strip():
            return await claim_once_then_die(
                engine=engine,
                persistence=persistence,
                settings=settings,
                revision=revision,
            )
        completions = build_deterministic_completions()
        execution = JobExecutionService(
            persistence,
            RunExecutor(create_builtin_handler_registry()),
            completions,
            utc_now,
            retry_policy=resolve_retry_policy(),
        )
        worker_id = generate_worker_id()
        registry = WorkerRegistry(persistence, worker_id, clock=utc_now)
        reclaimer = ReclaimLoop(LeaseReclaimer(persistence), clock=utc_now)
        worker = Worker(
            persistence,
            execution,
            completions,
            clock=utc_now,
            worker_id=worker_id,
            concurrency=settings.worker_concurrency,
            max_active=settings.max_active_jobs,
            registry=registry,
            reclaimer=reclaimer,
        )
        # Registration and the startup reclamation pass precede the readiness marker, so the
        # supervisor only observes a Worker that is durably registered and has swept once.
        registry.register()
        reclaimer.startup_pass()
        stop = asyncio.Event()
        installed = install_stop_handlers(asyncio.get_running_loop(), stop)
        try:
            marker: Path | None = settings.require_worker_ready_file()
            if marker is not None:
                write_ready_marker(marker, revision=revision, provider_ids=worker.provider_ids)
            logger.info("e2e_worker_ready worker_id=%s", worker.worker_id)
            await worker.run(stop)
        finally:
            for number in installed:
                with contextlib.suppress(ValueError, OSError, RuntimeError, NotImplementedError):
                    signal.signal(number, signal.SIG_DFL)
        return 0
    finally:
        engine.dispose()


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
