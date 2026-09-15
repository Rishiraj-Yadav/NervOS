"""Focused tests for the isolated E2E process supervisor."""

from __future__ import annotations

import importlib.util
import io
import sqlite3
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


def test_e2e_environment_strips_the_provider_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Automated verification must never be able to consume the operator's real credential."""
    e2e = load_module()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC-E2E-CREDENTIAL-DO-NOT-USE")
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC-SECOND-CREDENTIAL-DO-NOT-USE")

    environment = e2e.e2e_environment(tmp_path / "nervos-e2e.db", "http://127.0.0.1:5173")

    assert "ANTHROPIC_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert environment["NERVOS_ENVIRONMENT"] == "test"
    assert environment["NERVOS_DATABASE_PATH"] == str(tmp_path / "nervos-e2e.db")
    assert environment["NERVOS_APP_ORIGIN"] == "http://127.0.0.1:5173"
    assert environment["NERVOS_LOG_LEVEL"] == "WARNING"


def test_e2e_environment_is_complete_without_a_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    e2e = load_module()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    environment = e2e.e2e_environment(tmp_path / "nervos-e2e.db", "http://127.0.0.1:5173")

    assert "ANTHROPIC_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert environment["NERVOS_ENVIRONMENT"] == "test"


def test_e2e_launches_the_deterministic_factory_not_production_composition() -> None:
    """The supervised API must be the test-only factory, never the production entrypoint."""
    source = (ROOT / "scripts" / "e2e.py").read_text(encoding="utf-8")

    assert "e2e_app:create_app" in source
    assert "--factory" in source
    assert "tests/e2e_support" in source
    assert "nervos_api.main:app" not in source

    factory = ROOT / "tests" / "e2e_support" / "e2e_app.py"
    assert factory.is_file()
    assert "ANTHROPIC_API_KEY" not in factory.read_text(encoding="utf-8")


def test_development_launcher_still_uses_production_composition() -> None:
    """The dev launcher is pinned by tests/test_tooling.py and must not be repointed."""
    source = (ROOT / "scripts" / "dev.py").read_text(encoding="utf-8")

    assert "nervos_api.main:app" in source


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

        def poll(self) -> None:
            return None

    processes = iter(Process(name) for name in ("api", "worker", "web", "playwright"))

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

    def fake_worker_ready(*_args: object) -> str:
        return "schema_revision=test\nproviders=anthropic,openai\n"

    monkeypatch.setattr(e2e, "DEFAULT_DATABASE", default_database)
    monkeypatch.setattr(e2e.tempfile, "TemporaryDirectory", TemporaryDirectory)
    monkeypatch.setattr(e2e, "PortReservation", Reservation)
    monkeypatch.setattr(e2e, "resolve_required_command", resolve_required_command)
    monkeypatch.setattr(e2e.subprocess, "run", fake_migration)
    monkeypatch.setattr(e2e, "start_process", fake_start_process)
    monkeypatch.setattr(e2e, "wait_for_http_ready", wait_for_http_ready)
    monkeypatch.setattr(e2e, "wait_for_worker_ready", fake_worker_ready)
    monkeypatch.setattr(e2e, "terminate_process_tree", fake_terminate)

    with pytest.raises(RuntimeError, match="E2E modified the default database"):
        e2e.run_e2e()

    assert cleanup_order == ["playwright", "web", "worker", "api"]


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


def test_wait_for_worker_ready_reads_the_marker(tmp_path: Path) -> None:
    e2e = load_module()
    marker = tmp_path / "worker-ready.txt"
    marker.write_text("schema_revision=0003\nproviders=anthropic\n", encoding="utf-8")

    class Alive:
        def poll(self) -> None:
            return None

    assert (
        e2e.wait_for_worker_ready(marker, Alive(), e2e.WORKER_READY_TIMEOUT)
        == "schema_revision=0003\nproviders=anthropic\n"
    )


def test_wait_for_worker_ready_reports_an_early_exit(tmp_path: Path) -> None:
    e2e = load_module()

    class Exited:
        def poll(self) -> int:
            return 2

    with pytest.raises(RuntimeError, match="Worker exited before readiness with status 2"):
        e2e.wait_for_worker_ready(tmp_path / "absent.txt", Exited(), e2e.WORKER_READY_TIMEOUT)


def test_wait_for_worker_ready_times_out_when_no_marker_is_written(tmp_path: Path) -> None:
    e2e = load_module()

    class Alive:
        def poll(self) -> None:
            return None

    with pytest.raises(RuntimeError, match="Timed out waiting for the Worker readiness marker"):
        e2e.wait_for_worker_ready(tmp_path / "absent.txt", Alive(), 0.3)


def test_the_supervisor_launches_the_deterministic_worker_script() -> None:
    """The Worker is a third supervised process, driven by the test-only doubles."""
    source = (ROOT / "scripts" / "e2e.py").read_text(encoding="utf-8")

    assert "tests/e2e_support/e2e_worker.py" in source
    assert "NERVOS_WORKER_READY_FILE" in source
    assert "wait_for_worker_ready" in source

    worker = ROOT / "tests" / "e2e_support" / "e2e_worker.py"
    assert worker.is_file()
    text = worker.read_text(encoding="utf-8")
    # The deterministic doubles replace only the provider mapping; the loop is production code.
    assert "build_deterministic_completions" in text
    assert "nervos_worker.service" in text
    assert "ANTHROPIC_API_KEY" not in text


def test_the_retry_journey_is_scripted_outside_production_composition() -> None:
    """C4's scripted refusal and stretched wait live only in the supervised test plumbing."""
    supervisor = (ROOT / "scripts" / "e2e.py").read_text(encoding="utf-8")
    for variable in (
        "NERVOS_E2E_SCRIPTED_FAILURES",
        "NERVOS_E2E_SCRIPTED_CALL_LOG",
        "NERVOS_E2E_RETRY_DELAY_SECONDS",
        "NERVOS_E2E_RETRY_INPUT",
    ):
        assert variable in supervisor, variable
    assert "assert_retry_journey" in supervisor

    doubles = (ROOT / "tests" / "e2e_support" / "deterministic.py").read_text(encoding="utf-8")
    assert "MODEL_RATE_LIMITED" in doubles
    worker = (ROOT / "tests" / "e2e_support" / "e2e_worker.py").read_text(encoding="utf-8")
    assert "PRODUCTION_RETRY_POLICY" in worker

    # No shipped module may reach the scripting seam.
    for root in (ROOT / "apps", ROOT / "packages"):
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "SCRIPTED_FAILURES" not in text, path
            assert "NERVOS_E2E_RETRY_DELAY_SECONDS" not in text, path


def test_the_scripted_double_refuses_only_the_scripted_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The scripted refusal must hit exactly the prompt the supervisor named, and no other.

    The browser journey runs several prompts through one Worker, so a script that misfires would
    either retry an unscripted Run or quietly never refuse the scripted one — the second is
    exactly the failure mode that would let the C4 proof pass without a retry happening.
    """
    spec = importlib.util.spec_from_file_location(
        "nervos_deterministic", ROOT / "tests" / "e2e_support" / "deterministic.py"
    )
    assert spec is not None and spec.loader is not None
    doubles = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doubles)

    script = tmp_path / "failures.txt"
    script.write_text("anthropic\tretry me\t1\n", encoding="utf-8")
    calls = tmp_path / "calls.txt"
    monkeypatch.setenv(doubles.SCRIPTED_FAILURES_VARIABLE, str(script))
    monkeypatch.setenv(doubles.SCRIPTED_CALL_LOG_VARIABLE, str(calls))

    assert doubles._scripted_failure_count("anthropic", "retry me") == 1
    assert doubles._scripted_failure_count("anthropic", "another prompt") == 0
    assert doubles._scripted_failure_count("openai", "retry me") == 0

    assert doubles._record_scripted_call("anthropic", "retry me") == 0
    assert doubles._record_scripted_call("anthropic", "retry me") == 1
    assert doubles._record_scripted_call("anthropic", "another prompt") == 0
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "anthropic\tretry me",
        "anthropic\tretry me",
        "anthropic\tanother prompt",
    ]


def test_the_retry_journey_assertion_rejects_an_unretried_run(tmp_path: Path) -> None:
    """A Run that succeeded on its first Attempt must fail the C4 proof, not pass quietly."""
    module = load_module()
    database = tmp_path / "journey.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE runs (id INTEGER PRIMARY KEY, status TEXT, started_at TEXT,"
            " error_code TEXT, input_text TEXT)"
        )
        connection.execute("CREATE TABLE jobs (id INTEGER, run_id INTEGER, available_at TEXT)")
        connection.execute(
            "CREATE TABLE job_attempts (job_id INTEGER, attempt_number INTEGER, status TEXT,"
            " retry_disposition TEXT, error_code TEXT, claimed_at TEXT,"
            " execution_started_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE run_events (run_id INTEGER, sequence INTEGER, event_type TEXT)"
        )
        connection.execute(
            "INSERT INTO runs VALUES (1,'succeeded','t0',NULL,?)", (module.RETRY_INPUT,)
        )
        connection.execute("INSERT INTO jobs VALUES (1,1,'t0')")
        connection.execute("INSERT INTO job_attempts VALUES (1,1,'succeeded',NULL,NULL,'t0','t0')")
        connection.execute("INSERT INTO run_events VALUES (1,1,'run.succeeded')")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError):
        module.assert_retry_journey(database=database, scripted_log=tmp_path / "calls.txt")
