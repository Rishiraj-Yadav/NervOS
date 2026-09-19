"""Worker process lifecycle: schema gate, signals, execution loop, and the operator closeout.

The Worker never migrates the database. It validates the applied revision and refuses to start
against anything else, so a schema operated by two different versions of the code is impossible.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Sequence

from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobExecutionPersistence
from nervos_core.infrastructure.database.schema_revision import (
    SchemaRevisionMismatch,
    require_schema_revision,
)

from nervos_worker.app import (
    EXPECTED_SCHEMA_REVISION,
    close_worker,
    create_worker,
    reconcile_tool_definitions,
    utc_now,
    write_ready_marker,
)
from nervos_worker.config import WorkerSettings

logger = logging.getLogger("nervos_worker")


def configure_logging(level: str) -> None:
    """Configure one process-wide handler that never logs secrets or raw exception text."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
        force=True,
    )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="nervos_worker",
        description="Run the NervOS durable execution worker.",
    )
    parser.add_argument(
        "--reconcile-legacy-runs",
        action="store_true",
        help=(
            "Close pre-C2 running Runs that have no durable Job, then exit. "
            "Never stops to claim work, and is safe to run more than once."
        ),
    )
    return parser.parse_args(argv)


def reconcile_legacy_runs(settings: WorkerSettings) -> int:
    """Operator-invoked, idempotent closeout of legacy nonterminal Runs.

    This is deliberately not part of normal startup: during a mixed-version cutover an older API
    may still be executing a Run with no Job, and a Worker merely starting must not rewrite that
    in-flight Run underneath it.
    """
    engine = create_sqlite_engine(settings.database_path)
    try:
        require_schema_revision(engine, EXPECTED_SCHEMA_REVISION)
        persistence = SqlAlchemyJobExecutionPersistence(engine)
        closed = persistence.close_legacy_nonterminal_runs(now=utc_now())
        logger.info("legacy_closeout_complete closed=%s", closed)
        return 0
    finally:
        engine.dispose()


def install_stop_handlers(
    loop: asyncio.AbstractEventLoop, stop: asyncio.Event
) -> list[signal.Signals]:
    """Install the cross-platform stop path without assuming `add_signal_handler` exists."""

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


def remove_stop_handlers(installed: Sequence[signal.Signals]) -> None:
    for number in installed:
        with contextlib.suppress(ValueError, OSError, RuntimeError, NotImplementedError):
            signal.signal(number, signal.SIG_DFL)


async def run_worker(settings: WorkerSettings) -> int:
    """Compose, validate, run, and close one Worker process."""
    composition = create_worker(settings)
    stop = asyncio.Event()
    installed = install_stop_handlers(asyncio.get_running_loop(), stop)
    try:
        revision = require_schema_revision(composition.engine, EXPECTED_SCHEMA_REVISION)
        # The built-in tool definitions become durable only after the schema has been validated, so
        # a Worker that refuses to start cannot have written first.
        reconcile_tool_definitions(composition.engine)
        # Register a local source for every MCP connection that could contribute a tool. This reads
        # durable state only -- no MCP network I/O -- and it is what lets this process offer tools
        # another Worker or the control plane discovered. Registration is not authorization: D2's
        # live predicate still decides every call, so a stale registration is harmless.
        registered = composition.mcp_synchronizer.synchronize()
        logger.info("worker_mcp_sources_registered count=%s", registered)
        providers = composition.worker.provider_ids
        logger.info(
            "worker_started worker_id=%s schema_revision=%s concurrency=%s max_active=%s",
            composition.worker.worker_id,
            revision,
            settings.worker_concurrency,
            settings.max_active_jobs,
        )
        logger.info("worker_providers providers=%s", ",".join(providers) or "(none)")
        if not providers:
            # A Worker with no configured provider starts successfully and claims nothing.
            # Missing local capability is a deployment absence, never a Job failure.
            logger.warning("no_providers_configured configured=0")
        # Registration is mandatory: every executing incarnation must be durably observable, and
        # a Worker that cannot register must not claim. Registration creates no Job state.
        await asyncio.to_thread(composition.registry.register)
        # One bounded reclamation pass before readiness. It needs no provider, so a Worker with
        # no credential at all still reconciles work stranded by an earlier process.
        await asyncio.to_thread(composition.reclaimer.startup_pass)
        await asyncio.to_thread(composition.registry.log_summary)
        marker = settings.require_worker_ready_file()
        if marker is not None:
            write_ready_marker(marker, revision=revision, provider_ids=providers)
        await composition.worker.run(stop)
        logger.info("worker_stopped worker_id=%s", composition.worker.worker_id)
        return 0
    finally:
        remove_stop_handlers(installed)
        await close_worker(composition)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Worker, or the operator closeout, and return a process exit code."""
    arguments = parse_args(list(sys.argv[1:] if argv is None else argv))
    settings = WorkerSettings()
    configure_logging(settings.log_level)
    if arguments.reconcile_legacy_runs:
        return reconcile_legacy_runs(settings)
    try:
        return asyncio.run(run_worker(settings))
    except KeyboardInterrupt:
        logger.info("worker_interrupted")
        return 130
    except SchemaRevisionMismatch as error:
        print(str(error), file=sys.stderr)
        return 2
