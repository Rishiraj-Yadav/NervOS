"""Run the Stage A browser journey against isolated, supervised services."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = Path("~/.nervos/nervos.db").expanduser().resolve(strict=False)
API_READY_TIMEOUT = 15.0
WORKER_READY_TIMEOUT = 20.0
WEB_READY_TIMEOUT = 20.0
PLAYWRIGHT_TIMEOUT = 120.0
# Test-only lease granted to the pre-start crash claim, and the margin the supervisor waits
# past it before starting the recovery Worker. Bounded by construction: the claim lease is
# this short on purpose, so expiry is deterministic rather than a 60-second wait.
CRASH_LEASE_SECONDS = 1
RECOVERY_GRACE_SECONDS = 2
# C4 scripted-retry seam: the provider identifier and the exact prompt whose first call the
# deterministic double must refuse, plus how long the durable retry wait is stretched to. The
# wait must comfortably outlast a couple of two-second UI polls so the browser can observe the
# waiting Run, and stay far inside the bounded polling budget.
RETRY_PROVIDER = "anthropic"
RETRY_INPUT = "retry once before answering"
RETRY_DELAY_SECONDS = 6
# Every provider invocation in the whole journey: one for the recovered Run, one for the
# OpenAI Run, and two for the retried Run (its refused first Attempt and its succeeding second
# Attempt). The number is asserted globally on top of the per-prompt count in
# `assert_retry_journey`, so an unexpected extra invocation anywhere still fails the journey.
TOTAL_PROVIDER_CALLS = 4


@dataclass(frozen=True)
class FileFingerprint:
    """Observable state used to prove the default database was untouched."""

    exists: bool
    size: int | None = None
    modified_ns: int | None = None
    digest: str | None = None


class PortReservation:
    """Temporarily reserve one IPv4 loopback port."""

    def __init__(self) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.port = int(self.socket.getsockname()[1])

    def close(self) -> None:
        self.socket.close()


def fingerprint(path: Path) -> FileFingerprint:
    """Fingerprint a file without creating it."""
    if not path.exists():
        return FileFingerprint(exists=False)
    if not path.is_file():
        raise RuntimeError(f"Expected a file at {path}")
    stat = path.stat()
    return FileFingerprint(
        True, stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()
    )


def resolve_required_command(name: str) -> str:
    """Resolve an executable without invoking a platform shell."""
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"Required command '{name}' was not found")
    return executable


def start_process(
    command: list[str], environment: dict[str, str], log: IO[bytes]
) -> subprocess.Popen[bytes]:
    """Start one owned process tree with captured diagnostics."""
    if os.name == "nt":
        return subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            shell=False,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(
        command,
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        shell=False,
        start_new_session=True,
    )


def terminate_process_tree(process: subprocess.Popen[bytes]) -> bool:
    """Stop only the process tree rooted at an owned PID."""
    if os.name == "nt":
        taskkill = resolve_required_command("taskkill")
        subprocess.run(
            [taskkill, "/PID", str(process.pid), "/T"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        try:
            process.wait(timeout=5)
            return True
        except subprocess.TimeoutExpired:
            subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
            return True
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return False
    return True


def log_tail(path: Path, limit: int = 4000) -> str:
    """Read a bounded diagnostic tail from a service log."""
    if not path.exists():
        return ""
    return path.read_bytes()[-limit:].decode("utf-8", errors="replace")


def wait_for_http_ready(
    url: str,
    process: subprocess.Popen[bytes],
    timeout: float,
    validator: Callable[[int, bytes, str], bool],
) -> None:
    """Poll HTTP against a monotonic deadline while checking child liveness."""
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        returncode = process.poll()
        if returncode is not None:
            raise RuntimeError(f"Service exited before readiness with status {returncode}")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                body = response.read()
                content_type = response.headers.get("content-type", "")
                if validator(response.status, body, content_type):
                    return
                last_error = f"unexpected HTTP {response.status} response"
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = str(error)
        time.sleep(0.15)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def api_ready(status: int, body: bytes, _content_type: str) -> bool:
    """Require the exact health contract."""
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return status == 200 and payload == {"status": "ok"}


def wait_for_worker_ready(marker: Path, process: subprocess.Popen[bytes], timeout: float) -> str:
    """Wait for the Worker's readiness marker while checking child liveness.

    The Worker exposes no HTTP surface, so readiness is a file it writes only after settings
    load, schema validation, and provider resolution all succeed. A production Worker never
    sets the variable, so production never writes a file.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        returncode = process.poll()
        if returncode is not None:
            raise RuntimeError(f"Worker exited before readiness with status {returncode}")
        if marker.exists():
            return marker.read_text(encoding="utf-8")
        time.sleep(0.15)
    raise RuntimeError("Timed out waiting for the Worker readiness marker")


def web_ready(status: int, body: bytes, content_type: str) -> bool:
    """Require a successful HTML application response."""
    return status == 200 and "text/html" in content_type.lower() and bool(body)


def e2e_environment(database: Path, web_origin: str) -> dict[str, str]:
    """Build the child-process environment for an offline, credential-free E2E run.

    The provider credential is removed explicitly rather than merely left unused. An operator
    may legitimately have a real key exported in their shell, and automated verification must
    never construct or consume one — not even accidentally.
    """
    environment = os.environ.copy()
    environment.pop("ANTHROPIC_API_KEY", None)
    environment.pop("OPENAI_API_KEY", None)
    environment.update(
        {
            "NERVOS_ENVIRONMENT": "test",
            "NERVOS_DATABASE_PATH": str(database),
            "NERVOS_APP_ORIGIN": web_origin,
            "NERVOS_LOG_LEVEL": "WARNING",
        }
    )
    return environment


def run_e2e() -> int:
    """Migrate an isolated database, supervise services, and run Playwright."""
    original_database = fingerprint(DEFAULT_DATABASE)
    processes: list[subprocess.Popen[bytes]] = []
    cleanup_failed = False

    with tempfile.TemporaryDirectory(prefix="nervos-a5-e2e-") as directory:
        temporary = Path(directory)
        database = (temporary / "nervos-e2e.db").resolve()
        if database == DEFAULT_DATABASE:
            raise RuntimeError("E2E database resolved to the default NervOS database")
        api_log_path = temporary / "api.log"
        worker_log_path = temporary / "worker.log"
        vite_log_path = temporary / "vite.log"
        playwright_log_path = temporary / "playwright.log"
        worker_marker = temporary / "worker-ready.txt"
        # Test-only claim gate. The supervisor deliberately never creates this file: the
        # browser journey creates it after it has observed the queued Run, which makes the
        # queued state a deterministic precondition of execution instead of a race.
        worker_claim_gate = temporary / "worker-claim-gate.txt"
        # C3 pre-start recovery seam. Worker A claims one Job and exits before the
        # execution-start boundary; the supervisor waits for that marker and then starts
        # Worker B, whose startup reclamation pass must reconcile the expired claim.
        worker_crash_marker = temporary / "worker-crash.txt"
        worker_b_marker = temporary / "worker-b-ready.txt"
        provider_ledger = temporary / "provider-calls.txt"
        worker_b_log_path = temporary / "worker-b.log"
        # C4 durable-retry seam. The supervisor scripts exactly one refusal for one prompt, and
        # records every scripted call outside the repository so the assertion can prove how many
        # provider invocations that prompt actually produced.
        scripted_failures = temporary / "scripted-failures.txt"
        scripted_call_log = temporary / "scripted-calls.txt"
        scripted_failures.write_text(f"{RETRY_PROVIDER}\t{RETRY_INPUT}\t1\n", encoding="utf-8")
        api_reservation = PortReservation()
        web_reservation = PortReservation()
        api_port = api_reservation.port
        web_port = web_reservation.port
        api_origin = f"http://127.0.0.1:{api_port}"
        web_origin = f"http://127.0.0.1:{web_port}"
        environment = e2e_environment(database, web_origin)
        worker_environment = {
            **environment,
            "NERVOS_WORKER_READY_FILE": str(worker_marker),
            "NERVOS_E2E_CLAIM_GATE": str(worker_claim_gate),
            "NERVOS_E2E_PRE_START_CRASH": str(worker_crash_marker),
            "NERVOS_E2E_CLAIM_LEASE_SECONDS": str(CRASH_LEASE_SECONDS),
            "NERVOS_E2E_PROVIDER_CALL_LOG": str(provider_ledger),
        }
        # Worker B is the recovery Worker: it must claim freely, so it gets no claim gate.
        worker_b_environment = {
            **environment,
            "NERVOS_WORKER_READY_FILE": str(worker_b_marker),
            "NERVOS_E2E_PROVIDER_CALL_LOG": str(provider_ledger),
        }
        retry_environment = {
            "NERVOS_E2E_SCRIPTED_FAILURES": str(scripted_failures),
            "NERVOS_E2E_SCRIPTED_CALL_LOG": str(scripted_call_log),
            "NERVOS_E2E_RETRY_DELAY_SECONDS": str(RETRY_DELAY_SECONDS),
        }
        worker_environment = {**worker_environment, **retry_environment}
        worker_b_environment = {**worker_b_environment, **retry_environment}
        web_environment = {**environment, "NERVOS_E2E_API_ORIGIN": api_origin}
        playwright_environment = {
            **environment,
            "NERVOS_E2E_WEB_ORIGIN": web_origin,
            "NERVOS_E2E_CLAIM_GATE": str(worker_claim_gate),
            "NERVOS_E2E_RETRY_INPUT": RETRY_INPUT,
        }
        pnpm = resolve_required_command("pnpm")

        try:
            subprocess.run(
                [sys.executable, "-m", "alembic", "-c", "apps/api/alembic.ini", "upgrade", "head"],
                cwd=ROOT,
                env=environment,
                check=True,
                timeout=30,
                shell=False,
            )
            with (
                api_log_path.open("wb") as api_log,
                worker_log_path.open("wb") as worker_log,
                worker_b_log_path.open("wb") as worker_b_log,
                vite_log_path.open("wb") as vite_log,
                playwright_log_path.open("wb") as playwright_log,
            ):
                api_reservation.close()
                api = start_process(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "--factory",
                        "e2e_app:create_app",
                        "--app-dir",
                        "tests/e2e_support",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(api_port),
                        "--log-level",
                        "warning",
                    ],
                    environment,
                    api_log,
                )
                processes.append(api)
                try:
                    wait_for_http_ready(
                        f"{api_origin}/api/v1/health", api, API_READY_TIMEOUT, api_ready
                    )
                except RuntimeError as error:
                    raise RuntimeError(
                        f"API readiness failed: {error}\n{log_tail(api_log_path)}"
                    ) from error

                worker = start_process(
                    [sys.executable, "tests/e2e_support/e2e_worker.py"],
                    worker_environment,
                    worker_log,
                )
                processes.append(worker)
                try:
                    wait_for_worker_ready(worker_marker, worker, WORKER_READY_TIMEOUT)
                except RuntimeError as error:
                    raise RuntimeError(
                        f"Worker readiness failed: {error}\n{log_tail(worker_log_path)}"
                    ) from error

                web_reservation.close()
                web = start_process(
                    [
                        pnpm,
                        "--dir",
                        "apps/web",
                        "exec",
                        "vite",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(web_port),
                        "--strictPort",
                    ],
                    web_environment,
                    vite_log,
                )
                processes.append(web)
                try:
                    wait_for_http_ready(web_origin, web, WEB_READY_TIMEOUT, web_ready)
                except RuntimeError as error:
                    raise RuntimeError(
                        f"Vite readiness failed: {error}\n{log_tail(vite_log_path)}"
                    ) from error

                playwright = start_process(
                    [
                        pnpm,
                        "--dir",
                        "apps/web",
                        "exec",
                        "playwright",
                        "test",
                        "--project=chromium",
                        "--workers=1",
                    ],
                    playwright_environment,
                    playwright_log,
                )
                processes.append(playwright)
                # Watch for Worker A's pre-start loss on a side thread and bring up the recovery
                # Worker while the browser journey is still running. The delay between the two is
                # bounded by the test-only lease, never by luck: the crash claim uses
                # CRASH_LEASE_SECONDS. Playwright keeps its blocking wait, so the supervisor's
                # existing timeout contract is unchanged.
                recovery: dict[str, object] = {"started": False, "calls_before": -1}
                watcher = threading.Thread(
                    target=start_recovery_worker_when_crashed,
                    args=(
                        worker_crash_marker,
                        worker_b_marker,
                        worker_b_environment,
                        worker_b_log,
                        provider_ledger,
                        processes,
                        recovery,
                    ),
                    name="nervos-e2e-recovery",
                    daemon=True,
                )
                watcher.start()
                try:
                    returncode = playwright.wait(timeout=PLAYWRIGHT_TIMEOUT)
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError("Playwright exceeded its 120 second timeout") from error
                watcher.join(timeout=WORKER_READY_TIMEOUT)
                assert_retry_journey(database=database, scripted_log=scripted_call_log)
                assert_recovery_journey(
                    database=database,
                    worker_crash_marker=worker_crash_marker,
                    provider_ledger=provider_ledger,
                    calls_before_recovery=int(recovery["calls_before"]),  # type: ignore[arg-type]
                    worker_b_started=bool(recovery["started"]),
                    crash_log=log_tail(worker_log_path),
                )
                if returncode != 0:
                    print(log_tail(playwright_log_path), file=sys.stderr)
                return returncode
        finally:
            api_reservation.close()
            web_reservation.close()
            for process in reversed(processes):
                cleanup_failed = not terminate_process_tree(process) or cleanup_failed
            if fingerprint(DEFAULT_DATABASE) != original_database:
                raise RuntimeError(f"E2E modified the default database at {DEFAULT_DATABASE}")
            if cleanup_failed:
                raise RuntimeError("An owned E2E process tree survived cleanup")


def start_recovery_worker_when_crashed(
    crash_marker: Path,
    ready_marker: Path,
    environment: dict[str, str],
    log: IO[bytes],
    ledger: Path,
    processes: list[subprocess.Popen[bytes]],
    record: dict[str, object],
) -> None:
    """Wait for Worker A's pre-start loss, then start the Worker that must reclaim it.

    Runs on a side thread so Playwright keeps its blocking wait. The delay between the crash and
    the recovery Worker is bounded by the test-only lease plus a fixed margin, so expiry is
    deterministic rather than a race against the production 60-second lease.
    """
    deadline = time.monotonic() + PLAYWRIGHT_TIMEOUT
    while time.monotonic() < deadline:
        if crash_marker.exists():
            time.sleep(CRASH_LEASE_SECONDS + RECOVERY_GRACE_SECONDS)
            record["calls_before"] = count_provider_calls(ledger)
            worker = start_process(
                [sys.executable, "tests/e2e_support/e2e_worker.py"],
                environment,
                log,
            )
            processes.append(worker)
            wait_for_worker_ready(ready_marker, worker, WORKER_READY_TIMEOUT)
            record["started"] = True
            return
        time.sleep(0.05)


def count_provider_calls(ledger: Path) -> int:
    """Count provider invocations the deterministic doubles actually performed."""
    if not ledger.exists():
        return 0
    return len([line for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()])


def assert_recovery_journey(
    *,
    database: Path,
    worker_crash_marker: Path,
    provider_ledger: Path,
    calls_before_recovery: int,
    worker_b_started: bool,
    crash_log: str,
) -> None:
    """Prove the C3 pre-start recovery path actually happened, not just that the Run finished.

    The browser alone cannot distinguish "recovered then executed" from "never crashed", so the
    supervisor checks the durable timeline and the provider ledger directly.
    """
    if not worker_crash_marker.exists():
        raise RuntimeError(f"Worker A never claimed a Job before starting\n{crash_log}")
    if not worker_b_started:
        raise RuntimeError("Worker A crashed but the recovery Worker was never started")
    if calls_before_recovery != 0:
        raise RuntimeError(
            f"a provider was invoked {calls_before_recovery} time(s) before recovery; a Worker"
            " that died before the execution-start boundary must invoke none"
        )
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        first_run = connection.execute("SELECT min(id) FROM runs").fetchone()[0]
        timeline = [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM run_events WHERE run_id=? ORDER BY sequence",
                (first_run,),
            )
        ]
        claims = [
            row[0]
            for row in connection.execute(
                "SELECT attempt_number FROM job_attempts WHERE job_id="
                "(SELECT id FROM jobs WHERE run_id=?) ORDER BY attempt_number",
                (first_run,),
            )
        ]
        started = connection.execute(
            "SELECT count(*) FROM run_events WHERE run_id=? AND event_type='attempt.started'",
            (first_run,),
        ).fetchone()[0]
        abandoned = connection.execute(
            "SELECT execution_started_at FROM job_attempts WHERE job_id="
            "(SELECT id FROM jobs WHERE run_id=?) AND attempt_number=1",
            (first_run,),
        ).fetchone()
        status = connection.execute("SELECT status FROM runs WHERE id=?", (first_run,)).fetchone()[
            0
        ]
    finally:
        connection.close()

    expected = [
        "run.created",
        "run.queued",
        "attempt.claimed",
        "attempt.expired",
        "recovery.pre_start",
        "attempt.claimed",
        "attempt.started",
        "run.succeeded",
    ]
    if timeline != expected:
        raise RuntimeError(f"recovery timeline was {timeline}, expected {expected}")
    if claims != [1, 2]:
        raise RuntimeError(f"expected a second numbered Attempt after recovery, saw {claims}")
    if abandoned is None or abandoned[0] is not None:
        raise RuntimeError("the abandoned first Attempt crossed the execution-start boundary")
    if started != 1:
        raise RuntimeError(f"the recovered Run started execution {started} time(s), expected 1")
    if status != "succeeded" or count_provider_calls(provider_ledger) != TOTAL_PROVIDER_CALLS:
        raise RuntimeError(
            f"recovered Run status was {status!r} with"
            f" {count_provider_calls(provider_ledger)} provider call(s) overall,"
            f" expected {TOTAL_PROVIDER_CALLS}"
        )


def count_scripted_calls(log: Path, marker: str) -> int:
    """Count recorded scripted provider calls for one prompt marker."""
    if not log.exists():
        return 0
    return sum(1 for line in log.read_text(encoding="utf-8").splitlines() if line == marker)


def assert_retry_journey(
    *,
    database: Path,
    scripted_log: Path,
) -> None:
    """Prove the C4 durable retry actually happened, from committed state alone.

    A browser that merely shows a final answer cannot distinguish "retried safely" from "the
    provider answered the first time", so every claim here is checked against the durable
    timeline. The early-claim check is the load-bearing one: it compares the second Attempt's
    own `claimed_at` with the committed `available_at`, so it proves the retry waited for its
    due instant without depending on when a process happened to run.
    """
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        run = connection.execute(
            "SELECT id, status, started_at, error_code FROM runs WHERE input_text=?",
            (RETRY_INPUT,),
        ).fetchone()
        if run is None:
            raise RuntimeError("the C4 retry Run was never accepted")
        run_id, status, started_at, error_code = run
        timeline = [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM run_events WHERE run_id=? ORDER BY sequence", (run_id,)
            )
        ]
        attempts = [
            tuple(row)
            for row in connection.execute(
                "SELECT attempt_number, status, retry_disposition, error_code,"
                " claimed_at, execution_started_at FROM job_attempts WHERE job_id="
                "(SELECT id FROM jobs WHERE run_id=?) ORDER BY attempt_number",
                (run_id,),
            )
        ]
        due = connection.execute(
            "SELECT available_at FROM jobs WHERE run_id=?", (run_id,)
        ).fetchone()
    finally:
        connection.close()

    expected = [
        "run.created",
        "run.queued",
        "attempt.claimed",
        "attempt.started",
        "attempt.failed",
        "retry.scheduled",
        "attempt.claimed",
        "attempt.started",
        "run.succeeded",
    ]
    if timeline != expected:
        raise RuntimeError(f"retry timeline was {timeline}, expected {expected}")
    if status != "succeeded" or error_code is not None:
        raise RuntimeError(f"the retried Run ended as {status!r} ({error_code!r})")
    if len(attempts) != 2:
        raise RuntimeError(f"expected two Attempts for the retried Run, saw {len(attempts)}")
    first, second = attempts
    if first[0] != 1 or first[1] != "failed" or first[2] != "SAFE_TO_RETRY":
        raise RuntimeError(f"the first Attempt was not safe failure evidence: {first}")
    if first[3] != "model_rate_limited":
        raise RuntimeError(f"the first Attempt recorded {first[3]!r}")
    if second[0] != 2 or second[1] != "succeeded" or second[2] is not None:
        raise RuntimeError(f"the second Attempt was not a clean success: {second}")
    if second[5] is None or first[5] is None:
        raise RuntimeError("an Attempt never crossed the execution-start boundary")
    # The Run's start is the *first* execution's start; a retry must not rewrite it.
    if started_at != first[5] or second[5] <= first[5]:
        raise RuntimeError(
            f"the Run start {started_at!r} does not match the first Attempt ({first[5]!r})"
            f" with a later retry ({second[5]!r})"
        )
    if due is None or due[0] is None:
        raise RuntimeError("the retried Job has no durable due time")
    if second[4] < due[0]:
        raise RuntimeError(
            f"the retry was claimed at {second[4]!r}, before its due instant {due[0]!r}"
        )
    calls = count_scripted_calls(scripted_log, f"{RETRY_PROVIDER}\t{RETRY_INPUT}")
    if calls != 2:
        raise RuntimeError(f"the retried prompt made {calls} provider call(s), expected 2")


def main() -> int:
    """Run E2E and turn supervisor failures into concise diagnostics."""
    try:
        return run_e2e()
    except KeyboardInterrupt:
        print("E2E interrupted; owned process trees were stopped.", file=sys.stderr)
        return 130
    except (
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"E2E supervisor failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
