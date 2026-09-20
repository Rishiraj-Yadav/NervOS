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
from datetime import UTC, datetime
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

# C5 cancellation journey. One prompt whose provider call the double holds open until the
# supervisor releases it, so the browser can cancel a Run that is genuinely mid-call rather than
# merely slow. The supervisor bounds how long it waits for the Worker to *notice* the revoked
# authority: that discovery happens on the next heartbeat, so the bound is one production
# heartbeat interval plus slack, and production constants are never altered for the journey.
CANCEL_PROVIDER = "anthropic"
CANCEL_INPUT = "hold this call open until cancelled"
CANCEL_DISCOVERY_TIMEOUT_SECONDS = 40
# Every provider invocation in the whole journey: one for the recovered Run, one for the
# OpenAI Run, two for the retried Run (its refused first Attempt and its succeeding second
# Attempt), one for the cancelled Run, two for the tool-enabled Run (its tool-requesting turn
# and its concluding turn), and one for the E4 webhook Run. The number is asserted globally on
# top of the per-prompt counts in `assert_retry_journey` and `assert_cancellation_journey`, so an
# unexpected extra invocation anywhere — including a cancelled Run being executed again — still
# fails the journey.
TOTAL_PROVIDER_CALLS = 8

# Stage-D tool journey. One prompt whose scripted model turn asks for the one tool the supervisor
# grants, then concludes, so the browser observes a real tool lifecycle on the timeline. No grant
# management UI exists, so the supervisor seeds the grant directly; the argument JSON the double
# sends is fixed in `tests/e2e_support/deterministic.py` for the same built-in, so the seed and the
# script cannot drift apart.
TOOL_INPUT = "use the granted tool and then answer"
TOOL_UPSTREAM_NAME = "calculate"
TOOL_DEFINITION_VERSION = "2"
# How long the supervisor's seed thread waits for the browser to create the tool-enabled Agent
# Instance before giving up. It is bounded work on a side thread; the browser's own ack poll is
# what reports a seed that never happened.
TOOL_SEED_TIMEOUT_SECONDS = PLAYWRIGHT_TIMEOUT


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
        cancel_release = temporary / "cancel-release.txt"
        cancel_observed = temporary / "cancel-observed.txt"
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
        # Stage-D tool seam. The supervisor seeds the one tool grant as soon as the browser has
        # created the tool-enabled Agent Instance, and reports the seeded tool's durable
        # model-facing name here so the browser can assert it never reaches the rendered timeline.
        tool_grant_ack = temporary / "tool-grant-ack.txt"
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
            # C5 cancellation seam: the double holds this one prompt open until the supervisor
            # releases it, and records that the hold was cancelled out from under the Worker.
            "NERVOS_E2E_BLOCK_INPUT": CANCEL_INPUT,
            "NERVOS_E2E_BLOCK_RELEASE": str(cancel_release),
            "NERVOS_E2E_CANCEL_OBSERVED": str(cancel_observed),
            # Stage-D tool seam: this one prompt makes the double request the granted tool and then
            # conclude, so both Workers that may execute the tool-enabled Run are scripted alike.
            "NERVOS_E2E_TOOL_INPUT": TOOL_INPUT,
        }
        worker_environment = {**worker_environment, **retry_environment}
        worker_b_environment = {**worker_b_environment, **retry_environment}
        web_environment = {**environment, "NERVOS_E2E_API_ORIGIN": api_origin}
        playwright_environment = {
            **environment,
            "NERVOS_E2E_WEB_ORIGIN": web_origin,
            "NERVOS_E2E_CLAIM_GATE": str(worker_claim_gate),
            "NERVOS_E2E_RETRY_INPUT": RETRY_INPUT,
            "NERVOS_E2E_CANCEL_INPUT": CANCEL_INPUT,
            "NERVOS_E2E_TOOL_INPUT": TOOL_INPUT,
            "NERVOS_E2E_TOOL_GRANT_ACK": str(tool_grant_ack),
        }
        pnpm = resolve_required_command("pnpm")

        try:
            migration = subprocess.run(
                [sys.executable, "-m", "alembic", "-c", "apps/api/alembic.ini", "upgrade", "head"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                check=False,
                timeout=30,
                shell=False,
                text=True,
            )
            if migration.returncode != 0:
                raise RuntimeError(
                    "E2E migration failed with exit code "
                    f"{migration.returncode}:\n{migration.stdout}{migration.stderr}"
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
                # The tool-enabled Agent Instance is created mid-journey through the API, because no
                # UI exposes the definition version and no grant UI exists at all. This side thread
                # waits for that row, seeds the one grant, and acknowledges it so the browser only
                # submits the Run once the grant is committed and therefore inside its cutoff.
                tool_seed: dict[str, object] = {"seeded": False}
                tool_seeder = threading.Thread(
                    target=seed_tool_grant_when_agent_appears,
                    args=(
                        database,
                        tool_grant_ack,
                        TOOL_UPSTREAM_NAME,
                        TOOL_SEED_TIMEOUT_SECONDS,
                        tool_seed,
                    ),
                    name="nervos-e2e-tool-seed",
                    daemon=True,
                )
                tool_seeder.start()
                try:
                    returncode = playwright.wait(timeout=PLAYWRIGHT_TIMEOUT)
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError("Playwright exceeded its 120 second timeout") from error
                watcher.join(timeout=WORKER_READY_TIMEOUT)
                tool_seeder.join(timeout=WORKER_READY_TIMEOUT)
                if returncode != 0:
                    # Report the browser's own failure *before* any durable assertion. Otherwise
                    # a downstream state mismatch -- a Run left `running` because the journey
                    # never clicked, for instance -- masks the real Playwright error and sends
                    # the investigation in the wrong direction.
                    print(log_tail(playwright_log_path), file=sys.stderr)
                    raise RuntimeError(f"Playwright journey failed with status {returncode}")
                assert_tool_journey(
                    database=database,
                    tool_input=TOOL_INPUT,
                    tool_seeded=bool(tool_seed["seeded"]),
                )
                assert_cancellation_journey(
                    database=database,
                    cancel_observed=cancel_observed,
                    provider_ledger=provider_ledger,
                    release=cancel_release,
                )
                assert_retry_journey(database=database, scripted_log=scripted_call_log)
                assert_recovery_journey(
                    database=database,
                    worker_crash_marker=worker_crash_marker,
                    provider_ledger=provider_ledger,
                    calls_before_recovery=int(recovery["calls_before"]),  # type: ignore[arg-type]
                    worker_b_started=bool(recovery["started"]),
                    crash_log=log_tail(worker_log_path),
                )
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


def seed_tool_grant_when_agent_appears(
    database: Path,
    ack: Path,
    upstream_name: str,
    timeout: float,
    record: dict[str, object],
) -> None:
    """Grant one built-in tool to the tool-enabled Agent Instance the browser creates.

    No capability-management UI exists, so the grant is written durably, exactly as the reviewed
    operator action would be. It runs on a side thread because the browser creates the Agent
    Instance mid-journey: the thread waits for that row, commits the grant, and only then writes the
    acknowledgment the browser polls. The Run is therefore submitted *after* the grant is committed,
    which is what lets its submission-time cutoff admit the grant at call time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            model_name = _grant_builtin_tool(database, upstream_name)
        except (sqlite3.Error, OSError):
            # A concurrent API/Worker write may hold the database briefly; retry rather than fail.
            model_name = None
        if model_name is not None:
            ack.write_text(f"{model_name}\n", encoding="utf-8")
            record["seeded"] = True
            return
        time.sleep(0.15)


def _grant_builtin_tool(database: Path, upstream_name: str) -> str | None:
    """Insert the one grant for the first tool-enabled Agent Instance, or return None to retry.

    Returns ``None`` while the Agent Instance or the reconciled built-in definition does not exist
    yet. The reviewed fingerprint is copied from the durable definition inside the same statement,
    so the grant can never record a fingerprint the definition does not currently have, and an
    already-present grant is left untouched rather than duplicated.
    """
    connection = sqlite3.connect(database, timeout=5.0)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        agent = connection.execute(
            "SELECT id FROM agent_instances WHERE agent_definition_version = ? ORDER BY id LIMIT 1",
            (TOOL_DEFINITION_VERSION,),
        ).fetchone()
        if agent is None:
            return None
        definition = connection.execute(
            "SELECT id, model_name FROM tool_definitions"
            " WHERE source_kind = 'builtin' AND upstream_name = ? AND status = 'available'",
            (upstream_name,),
        ).fetchone()
        if definition is None:
            return None
        existing = connection.execute(
            "SELECT id FROM agent_tool_grants"
            " WHERE agent_instance_id = ? AND tool_definition_id = ?",
            (agent[0], definition[0]),
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO agent_tool_grants"
                " (agent_instance_id, tool_definition_id, reviewed_fingerprint, created_at)"
                " SELECT ?, id, fingerprint, ? FROM tool_definitions WHERE id = ?",
                (agent[0], _utc_storage_text(), definition[0]),
            )
            connection.commit()
        return str(definition[1])
    finally:
        connection.close()


def _utc_storage_text() -> str:
    """Format the current instant the way SQLAlchemy stores naive UTC datetimes in SQLite."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def assert_tool_journey(*, database: Path, tool_input: str, tool_seeded: bool) -> None:
    """Prove the Stage-D tool lifecycle from committed state alone.

    A browser can only show that *a* timeline rendered. This checks the durable truth behind it: the
    Run snapshotted a tool budget and a live grant cutoff, exactly one granted built-in was invoked,
    it reached `succeeded` rather than being denied or left ambiguous, and the timeline is the
    frozen lifecycle with nothing invented to fill a gap.
    """
    if not tool_seeded:
        raise RuntimeError("the tool grant was never seeded for the tool-enabled Agent Instance")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        run = connection.execute(
            "SELECT id, status, output_text, error_code, max_tool_calls, tool_grant_cutoff_id"
            " FROM runs WHERE input_text = ?",
            (tool_input,),
        ).fetchone()
        if run is None:
            raise RuntimeError("the tool-enabled Run was never accepted")
        run_id, status, output_text, error_code, max_tool_calls, cutoff = run
        timeline = [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM run_events WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            )
        ]
        invocations = [
            tuple(row)
            for row in connection.execute(
                "SELECT upstream_name, status, started_at FROM tool_invocations"
                " WHERE run_id = ? ORDER BY tool_sequence",
                (run_id,),
            )
        ]
    finally:
        connection.close()

    if max_tool_calls <= 0 or cutoff <= 0:
        raise RuntimeError(
            f"the tool-enabled Run snapshotted max_tool_calls={max_tool_calls!r} with cutoff"
            f" {cutoff!r}; a tool Run needs a positive budget and a live grant cutoff"
        )
    expected = [
        "run.created",
        "run.queued",
        "attempt.claimed",
        "attempt.started",
        "tool.requested",
        "tool.started",
        "tool.succeeded",
        "run.succeeded",
    ]
    if timeline != expected:
        raise RuntimeError(f"tool timeline was {timeline}, expected {expected}")
    if status != "succeeded" or error_code is not None or output_text is None:
        raise RuntimeError(
            f"the tool-enabled Run ended as {status!r} with error {error_code!r}"
            f" and output {'set' if output_text is not None else 'unset'}"
        )
    if len(invocations) != 1:
        raise RuntimeError(f"expected one tool invocation for the tool Run, saw {len(invocations)}")
    upstream_name, invocation_status, started_at = invocations[0]
    if upstream_name != TOOL_UPSTREAM_NAME:
        raise RuntimeError(
            f"the tool Run invoked {upstream_name!r}, expected {TOOL_UPSTREAM_NAME!r}"
        )
    if invocation_status != "succeeded" or started_at is None:
        raise RuntimeError(
            f"the tool invocation was {invocation_status!r} with started_at={started_at!r}"
        )


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


def wait_for_cancellation_observation(observed: Path) -> bool:
    """Wait for the Worker to notice a revoked authority, on its own heartbeat cadence.

    Discovery is deliberately not instantaneous: cancellation is authoritative the moment the API
    commits it, but the *local task* is stopped when the Worker's next renewal matches zero rows.
    The supervisor therefore waits out the shipped heartbeat interval rather than altering it, so
    the journey never weakens the lease relationship that C3's recovery proofs depend on.
    """
    deadline = time.monotonic() + CANCEL_DISCOVERY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if observed.exists() and observed.read_text(encoding="utf-8").strip():
            return True
        time.sleep(0.25)
    return False


def assert_cancellation_journey(
    *,
    database: Path,
    cancel_observed: Path,
    provider_ledger: Path,
    release: Path,
) -> None:
    """Prove the C5 cancellation from committed state alone.

    Every claim here is checked against durable rows, not against Worker logs: the Run is
    `cancelled` with no fabricated start or duration, its Job carries the cancellation request
    exactly once, its Attempt is cancelled with its real start preserved, the cancellation
    timeline is exactly two events with no intermediate Run failure, and the provider was called
    exactly once for it. The observed-hold ledger is the one piece of Worker-side evidence, and
    it is what distinguishes "NervOS stopped waiting" from "the call had not answered yet".
    """
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT id, status, started_at, finished_at, elapsed_ms, output_text, error_code"
            " FROM runs WHERE input_text=?",
            (CANCEL_INPUT,),
        ).fetchone()
        if row is None:
            raise RuntimeError("the cancellation journey left no Run for its scripted prompt")
        run_id, status, started_at, finished_at, elapsed_ms, output_text, error_code = row
        if status != "cancelled":
            raise RuntimeError(f"the cancelled Run settled as {status!r}, expected 'cancelled'")
        if started_at is None:
            raise RuntimeError("the cancelled Run lost the start boundary it really had")
        if finished_at is None or elapsed_ms is None:
            raise RuntimeError("the cancelled Run has no truthful finish boundary or duration")
        if output_text is not None:
            raise RuntimeError("a cancelled Run must never carry output")
        if error_code is not None:
            raise RuntimeError("a cancelled Run is not a provider failure and must carry no error")

        job = connection.execute(
            "SELECT status, cancel_requested_at, error_code, claimed_by, claim_token"
            " FROM jobs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        job_status, cancel_requested_at, job_error, claimed_by, claim_token = job
        if job_status != "cancelled" or cancel_requested_at is None:
            raise RuntimeError("the Run's Job is not a durably requested cancellation")
        if job_error != "execution_cancelled":
            raise RuntimeError(f"the cancelled Job reported {job_error!r}")
        if claimed_by is not None or claim_token is not None:
            raise RuntimeError("cancellation must release the Job's claim authority")

        attempts = connection.execute(
            "SELECT status, execution_started_at FROM job_attempts WHERE job_id="
            "(SELECT id FROM jobs WHERE run_id=?) ORDER BY attempt_number",
            (run_id,),
        ).fetchall()
        if len(attempts) != 1:
            raise RuntimeError(f"cancellation invented {len(attempts)} Attempts, expected 1")
        attempt_status, execution_started_at = attempts[0]
        if attempt_status != "cancelled":
            raise RuntimeError(f"the Attempt settled as {attempt_status!r}, expected 'cancelled'")
        if execution_started_at is None:
            raise RuntimeError("the cancelled Attempt lost its real execution start")

        timeline = [
            event[0]
            for event in connection.execute(
                "SELECT event_type FROM run_events WHERE run_id=? ORDER BY sequence", (run_id,)
            )
        ]
        if timeline[-2:] != ["cancellation.requested", "run.cancelled"]:
            raise RuntimeError(f"unexpected cancellation timeline: {timeline}")
        if "run.succeeded" in timeline or "run.failed" in timeline:
            raise RuntimeError(f"cancellation fabricated a terminal outcome: {timeline}")
        if "retry.scheduled" in timeline:
            raise RuntimeError("a cancelled Run must never schedule a retry")
    finally:
        connection.close()

    # Discovery is not instantaneous, and deliberately so: the durable cancellation landed the
    # moment the API committed it, but the *local* task stops when the Worker's next renewal
    # matches zero rows. Wait on the shipped cadence rather than shortening it.
    if not wait_for_cancellation_observation(cancel_observed):
        raise RuntimeError(
            "the Worker never stopped its blocked provider call within"
            f" {CANCEL_DISCOVERY_TIMEOUT_SECONDS}s: cancellation was durable but the local task"
            " was not observed to be cancelled"
        )
    if count_provider_calls(provider_ledger) != TOTAL_PROVIDER_CALLS:
        raise RuntimeError(
            "the cancellation journey changed the total provider invocation count:"
            f" expected {TOTAL_PROVIDER_CALLS}, found {count_provider_calls(provider_ledger)}"
        )
    # Release the hold so the Worker's abandoned task can finish during cleanup.
    release.write_text("released\n", encoding="utf-8")


def count_provider_calls_for(ledger: Path) -> int:
    """Count cancellation observations recorded by the blocked deterministic double."""
    return len([line for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()])


if __name__ == "__main__":
    raise SystemExit(main())
