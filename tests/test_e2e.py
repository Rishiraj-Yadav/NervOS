"""Focused tests for the isolated E2E process supervisor."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module() -> ModuleType:
    path = ROOT / "scripts" / "e2e.py"
    spec = importlib.util.spec_from_file_location("nervos_e2e", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load E2E supervisor at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_fingerprint_detects_creation_and_changes(tmp_path: Path) -> None:
    e2e = load_module()
    target = tmp_path / "database.db"

    missing = e2e.fingerprint(target)
    target.write_bytes(b"first")
    first = e2e.fingerprint(target)
    target.write_bytes(b"second")

    assert missing.exists is False
    assert first.exists is True
    assert first.digest != e2e.fingerprint(target).digest


def test_run_e2e_rejects_a_temporary_database_that_aliases_the_default_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e2e = load_module()
    default_database = tmp_path / "nervos-e2e.db"
    started = False

    class TemporaryDirectory:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> str:
            return str(tmp_path)

        def __exit__(self, *_args: object) -> None:
            return None

    def fail_if_started(*_args: object, **_kwargs: object) -> None:
        nonlocal started
        started = True
        raise AssertionError("E2E subprocess work must not start before the database guard")

    monkeypatch.setattr(e2e, "DEFAULT_DATABASE", default_database)
    monkeypatch.setattr(e2e.tempfile, "TemporaryDirectory", TemporaryDirectory)
    monkeypatch.setattr(e2e, "resolve_required_command", fail_if_started)
    monkeypatch.setattr(e2e, "start_process", fail_if_started)

    with pytest.raises(RuntimeError, match="E2E database resolved to the default NervOS database"):
        e2e.run_e2e()

    assert started is False


def test_run_e2e_detects_default_database_mutation_after_owned_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e2e = load_module()
    default_database = tmp_path / "default.db"
    default_database.write_bytes(b"before")
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    cleanup_order: list[str] = []

    class TemporaryDirectory:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> str:
            return str(temporary)

        def __exit__(self, *_args: object) -> None:
            return None

    class Reservation:
        def __init__(self) -> None:
            self.port = 41000 + len(cleanup_order)

        def close(self) -> None:
            return None

    class Process:
        def __init__(self, name: str) -> None:
            self.name = name

        def wait(self, timeout: float | None = None) -> int:
            assert timeout == e2e.PLAYWRIGHT_TIMEOUT
            assert self.name == "playwright"
            default_database.write_bytes(b"after")
            return 0

    processes = iter(Process(name) for name in ("api", "web", "playwright"))

    def fake_start_process(*_args: object, **_kwargs: object) -> Process:
        return next(processes)

    def fake_terminate(process: Process) -> bool:
        cleanup_order.append(process.name)
        return True

    def fake_migration(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess([], 0)

    def resolve_required_command(name: str) -> str:
        return f"{name}.exe"

    def wait_for_http_ready(*_args: object) -> None:
        return None

    monkeypatch.setattr(e2e, "DEFAULT_DATABASE", default_database)
    monkeypatch.setattr(e2e.tempfile, "TemporaryDirectory", TemporaryDirectory)
    monkeypatch.setattr(e2e, "PortReservation", Reservation)
    monkeypatch.setattr(e2e, "resolve_required_command", resolve_required_command)
    monkeypatch.setattr(e2e.subprocess, "run", fake_migration)
    monkeypatch.setattr(e2e, "start_process", fake_start_process)
    monkeypatch.setattr(e2e, "wait_for_http_ready", wait_for_http_ready)
    monkeypatch.setattr(e2e, "terminate_process_tree", fake_terminate)

    with pytest.raises(RuntimeError, match="E2E modified the default database"):
        e2e.run_e2e()

    assert cleanup_order == ["playwright", "web", "api"]


def test_port_reservation_uses_ipv4_loopback() -> None:
    e2e = load_module()
    reservation = e2e.PortReservation()
    try:
        assert reservation.socket.getsockname() == ("127.0.0.1", reservation.port)
    finally:
        reservation.close()


def test_start_process_never_uses_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    e2e = load_module()
    sentinel = object()
    captured: dict[str, object] = {}

    def fake_popen(command: list[str], **kwargs: object) -> object:
        captured.update(kwargs)
        assert command == ["tool", "serve"]
        return sentinel

    monkeypatch.setattr(e2e.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(e2e.os, "name", "posix")

    assert e2e.start_process(["tool", "serve"], {"KEY": "value"}, io.BytesIO()) is sentinel
    assert captured["shell"] is False
    assert captured["start_new_session"] is True
    assert captured["cwd"] == ROOT


def test_readiness_validators_require_semantic_responses() -> None:
    e2e = load_module()

    assert e2e.api_ready(200, b'{"status":"ok"}', "application/json") is True
    assert e2e.api_ready(200, b'{"status":"wrong"}', "application/json") is False
    assert e2e.web_ready(200, b"<html></html>", "text/html; charset=utf-8") is True
    assert e2e.web_ready(404, b"missing", "text/html") is False


def test_wait_for_http_ready_fails_when_process_exits() -> None:
    e2e = load_module()

    class ExitedProcess:
        def poll(self) -> int:
            return 23

    with pytest.raises(RuntimeError, match="status 23"):
        e2e.wait_for_http_ready(
            "http://127.0.0.1:1",
            ExitedProcess(),
            timeout=0.01,
            validator=e2e.api_ready,
        )


def test_windows_cleanup_targets_owned_tree_and_escalates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e2e = load_module()
    calls: list[list[str]] = []

    class Process:
        pid = 321
        waits = 0

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("owned", 5)
            return 0

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["shell"] is False
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    def fake_resolve(name: str) -> str:
        assert name == "taskkill"
        return "taskkill.exe"

    monkeypatch.setattr(e2e.os, "name", "nt")
    monkeypatch.setattr(e2e, "resolve_required_command", fake_resolve)
    monkeypatch.setattr(e2e.subprocess, "run", fake_run)

    assert e2e.terminate_process_tree(Process()) is True
    assert calls == [
        ["taskkill.exe", "/PID", "321", "/T"],
        ["taskkill.exe", "/PID", "321", "/T", "/F"],
    ]


def test_windows_cleanup_targets_tree_even_after_parent_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e2e = load_module()
    calls: list[list[str]] = []

    class ExitedProcess:
        pid = 987

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["shell"] is False
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    def fake_resolve(name: str) -> str:
        assert name == "taskkill"
        return "taskkill.exe"

    monkeypatch.setattr(e2e.os, "name", "nt")
    monkeypatch.setattr(e2e, "resolve_required_command", fake_resolve)
    monkeypatch.setattr(e2e.subprocess, "run", fake_run)

    assert e2e.terminate_process_tree(ExitedProcess()) is True
    assert calls == [["taskkill.exe", "/PID", "987", "/T"]]


def test_posix_cleanup_targets_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    e2e = load_module()
    signals: list[tuple[int, int]] = []

    class Process:
        pid = 654

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

    def record_signal(pid: int, sent_signal: int) -> None:
        signals.append((pid, sent_signal))

    monkeypatch.setattr(e2e.os, "name", "posix")
    monkeypatch.setattr(e2e.os, "killpg", record_signal, raising=False)

    assert e2e.terminate_process_tree(Process()) is True
    assert signals == [(654, e2e.signal.SIGTERM)]
