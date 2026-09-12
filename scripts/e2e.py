"""Run the Stage A browser journey against isolated, supervised services."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
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
WEB_READY_TIMEOUT = 20.0
PLAYWRIGHT_TIMEOUT = 120.0


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


def web_ready(status: int, body: bytes, content_type: str) -> bool:
    """Require a successful HTML application response."""
    return status == 200 and "text/html" in content_type.lower() and bool(body)


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
        vite_log_path = temporary / "vite.log"
        playwright_log_path = temporary / "playwright.log"
        api_reservation = PortReservation()
        web_reservation = PortReservation()
        api_port = api_reservation.port
        web_port = web_reservation.port
        api_origin = f"http://127.0.0.1:{api_port}"
        web_origin = f"http://127.0.0.1:{web_port}"
        environment = os.environ.copy()
        environment.update(
            {
                "NERVOS_ENVIRONMENT": "test",
                "NERVOS_DATABASE_PATH": str(database),
                "NERVOS_APP_ORIGIN": web_origin,
                "NERVOS_LOG_LEVEL": "WARNING",
            }
        )
        web_environment = {**environment, "NERVOS_E2E_API_ORIGIN": api_origin}
        playwright_environment = {**environment, "NERVOS_E2E_WEB_ORIGIN": web_origin}
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
                vite_log_path.open("wb") as vite_log,
                playwright_log_path.open("wb") as playwright_log,
            ):
                api_reservation.close()
                api = start_process(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "nervos_api.main:app",
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
                try:
                    returncode = playwright.wait(timeout=PLAYWRIGHT_TIMEOUT)
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError("Playwright exceeded its 120 second timeout") from error
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
