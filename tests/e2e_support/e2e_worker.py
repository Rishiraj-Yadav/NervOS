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

from deterministic import build_deterministic_completions
from nervos_core.application.job_execution import ClaimedAttempt, JobExecutionService
from nervos_core.application.run_execution import RunExecutor
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobExecutionPersistence

# The Worker application itself is production code; only the provider mapping is a double.
from nervos_worker.app import require_schema_revision, write_ready_marker
from nervos_worker.config import WorkerSettings
from nervos_worker.identity import generate_worker_id
from nervos_worker.service import Worker
from sqlalchemy.engine import Engine

logger = logging.getLogger("e2e_worker")

# Test-only claim gate. The supervisor points this at a path inside its own temporary
# directory and never creates it; the browser journey creates it once it has observed the
# queued Run. Only this script reads it — no production module and no production setting
# knows it exists, so the shipped Worker always claims immediately.
CLAIM_GATE_VARIABLE = "NERVOS_E2E_CLAIM_GATE"


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
        completions = build_deterministic_completions()
        execution = JobExecutionService(
            persistence,
            RunExecutor(create_builtin_handler_registry()),
            completions,
            utc_now,
        )
        worker = Worker(
            persistence,
            execution,
            completions,
            clock=utc_now,
            worker_id=generate_worker_id(),
            concurrency=settings.worker_concurrency,
            max_active=settings.max_active_jobs,
        )
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
