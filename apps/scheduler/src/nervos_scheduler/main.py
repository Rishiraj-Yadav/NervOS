"""Scheduler process lifecycle: schema gate, signals, poll loop, and closeout.

The Scheduler never migrates the database. It validates the applied revision and refuses to start
against anything else, so a database operated by two different versions of the code stays
impossible, and the Scheduler specifically cannot write a Run against a schema it does not
understand.

The loop is deliberately synchronous. Everything it does is a bounded local database read followed
by one short local transaction; there is no network, no provider call and no tool execution, so
there is no I/O to keep off the main thread and no concurrency to manage.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import signal
import sys
import threading
from collections.abc import Sequence

from nervos_core.application.scheduler import SCHEDULER_POLL_INTERVAL_SECONDS
from nervos_core.infrastructure.database.schema_revision import (
    SchemaRevisionMismatch,
    require_schema_revision,
)

from nervos_scheduler.app import (
    EXPECTED_SCHEMA_REVISION,
    SchedulerComposition,
    close_scheduler,
    create_scheduler,
    utc_now,
    write_ready_marker,
)
from nervos_scheduler.config import SchedulerSettings

logger = logging.getLogger("nervos_scheduler")


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
        prog="nervos_scheduler",
        description="Run the NervOS schedule evaluator.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Run a single tick and exit. Intended for operators and diagnostics; it is not "
            "a substitute for the long-running process, which is what keeps schedules firing."
        ),
    )
    return parser.parse_args(argv)


def install_stop_handler(stop: threading.Event) -> list[signal.Signals]:
    """Arrange for SIGINT and SIGTERM to end the loop at the next opportunity."""

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    installed: list[signal.Signals] = []
    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        with contextlib.suppress(ValueError, OSError, RuntimeError, NotImplementedError):
            signal.signal(number, request_stop)
            installed.append(number)
    return installed


def remove_stop_handler(installed: Sequence[signal.Signals]) -> None:
    for number in installed:
        with contextlib.suppress(ValueError, OSError, RuntimeError, NotImplementedError):
            signal.signal(number, signal.SIG_DFL)


def run_scheduler(settings: SchedulerSettings, *, once: bool = False) -> int:
    """Compose, validate, run, and close one Scheduler process.

    The startup order matters in one specific way: the schema gate runs **before** the composition
    is built, so a process pointed at the wrong revision fails before it can read or write anything.
    """
    composition = create_scheduler(settings)
    stop = threading.Event()
    installed = install_stop_handler(stop)
    try:
        revision = require_schema_revision(composition.engine, EXPECTED_SCHEMA_REVISION)
        logger.info(
            "scheduler_started schema_revision=%s poll_interval_seconds=%s",
            revision,
            SCHEDULER_POLL_INTERVAL_SECONDS,
        )
        # One immediate tick, then the loop. This is the only unconditional scan: a tick that ran
        # before the wait and again straight after it would double the work for every start and
        # make the tick log useless for telling how often the scan actually happens.
        run_tick(composition)
        marker = settings.require_scheduler_ready_file()
        if marker is not None:
            write_ready_marker(marker, revision=revision)
        if once:
            return 0
        while not stop.is_set():
            # The wait *is* the poll interval, and it is interruptible: a stop request ends the
            # process immediately rather than after up to five seconds. No transaction is open
            # here, and no tick is ever cut short by a shutdown.
            if stop.wait(SCHEDULER_POLL_INTERVAL_SECONDS):
                break
            run_tick(composition)
        logger.info("scheduler_stopped")
        return 0
    finally:
        remove_stop_handler(installed)
        close_scheduler(composition)


def run_tick(composition: SchedulerComposition) -> None:
    """Run one tick and log its counters. This is the only line the loop adds to the service."""
    tick = composition.service.tick(utc_now())
    logger.info(
        "scheduler_tick examined=%s materialized=%s skipped=%s duplicated=%s stale=%s "
        "deferred_capacity=%s deferred_contention=%s",
        tick.examined,
        tick.materialized,
        tick.skipped,
        tick.duplicated,
        tick.stale,
        tick.deferred_capacity,
        tick.deferred_contention,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Scheduler and return a process exit code."""
    arguments = parse_args(list(sys.argv[1:] if argv is None else argv))
    settings = SchedulerSettings()
    configure_logging(settings.log_level)
    try:
        return run_scheduler(settings, once=arguments.once)
    except KeyboardInterrupt:
        logger.info("scheduler_interrupted")
        return 130
    except SchemaRevisionMismatch as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
