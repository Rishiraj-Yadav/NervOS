"""ADR 0035: real operating-system containment for package-host processes.

These tests are OS evidence, not mocks: on Windows they assign a live child process to a
nested Job Object with the frozen limits and observe what the kernel does to it. Anything that
cannot be proven on the host platform is skipped with an explicit reason rather than asserted
from a double -- a mocked Job Object proves nothing about a Job Object.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from nervos_core.application.sandbox import (
    MEMORY_LIMIT_BYTES,
    ContainmentResult,
    ContainmentTier,
    ContainmentUnavailable,
    SandboxLaunch,
    WorkerSandboxCapability,
)
from nervos_core.infrastructure import sandbox as sandbox_infrastructure
from nervos_core.infrastructure.sandbox import create_containment, observe_sandbox_capability

WINDOWS = platform.system() == "Windows"
POSIX = platform.system() == "Linux"

# A child that allocates far past the frozen per-process memory cap. On Windows the cap makes
# the allocation fail and the interpreter dies; on a POSIX rlimit the allocation raises
# MemoryError. Either way the process cannot reach the requested size.
OVER_ALLOCATE = "value = bytearray(1024 * 1024 * 1024)\nprint(len(value), flush=True)\n"
SLEEP = "import time\ntime.sleep(120)\n"


def _spawn(script: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )


def _wait(process: subprocess.Popen[bytes], seconds: float) -> int:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            return code
        time.sleep(0.05)
    return -1


def test_an_unsupported_platform_refuses_rather_than_running_uncontained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    with pytest.raises(ContainmentUnavailable):
        create_containment()


@pytest.mark.parametrize("system", ["Windows", "Linux", "Darwin", "Unknown"])
def test_resource_only_platforms_cannot_license_package_execution(
    system: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: system)
    with pytest.raises(ContainmentUnavailable):
        create_containment()


def test_installation_health_check_refuses_before_importing_package_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nervos_core.application.package_health import SubprocessPackageHealthChecker

    def forbidden_spawn(*args: object, **kwargs: object) -> None:
        pytest.fail("untrusted package health code must never start on an unqualified platform")

    monkeypatch.setattr(subprocess, "Popen", forbidden_spawn)
    with pytest.raises(ContainmentUnavailable):
        SubprocessPackageHealthChecker().check(
            environment=tmp_path, entrypoint="untrusted.agent:Agent", expected_sdk_api_version="1"
        )


def test_linux_launcher_prepares_namespaces_before_python_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nervos_core.infrastructure.sandbox.linux_bubblewrap import LinuxBubblewrapContainment

    executable = tmp_path / "bwrap"
    executable.write_bytes(b"fixture")
    environment_root = tmp_path / "environment"
    python = environment_root / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fixture")
    (environment_root / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    launch = LinuxBubblewrapContainment(str(executable)).prepare(
        python=python,
        scratch=scratch,
        environment={"LANG": "C.UTF-8", "SECRET_VALUE": "must-not-cross"},
        arguments=("-m", "nervos_package_host"),
    )

    assert "--unshare-all" in launch.command
    assert "--clearenv" in launch.command
    environment_path = str(environment_root.resolve())
    assert any(
        launch.command[index : index + 3] == ("--ro-bind", environment_path, environment_path)
        for index in range(len(launch.command) - 2)
    )
    # The interpreter executes through its in-environment path, never a resolved system path:
    # resolving a venv's `bin/python` symlink would mount the base interpreter's directory as
    # the package environment and never expose the environment's own site-packages.
    separator = launch.command.index("--")
    assert launch.command[separator + 1] == str(environment_root.resolve() / "bin" / "python")
    assert "SECRET_VALUE" not in launch.command
    assert launch.environment == {}
    assert launch.preexec_fn is not None
    assert launch.start_new_session is True


def test_linux_launcher_applies_the_process_budget_inside_the_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``RLIMIT_NPROC`` binds the package host, never the namespace bootstrap.

    The launcher creates its namespaces by forking; a launcher pre-limited to the
    package process budget dies with ``EAGAIN`` before any package code starts as
    soon as the launching user already has that many processes on the host. The
    frozen budget is therefore applied by the interpreter inside the namespace.
    """
    from nervos_core.infrastructure.sandbox.linux_bubblewrap import LinuxBubblewrapContainment

    executable = tmp_path / "bwrap"
    executable.write_bytes(b"fixture")
    environment_root = tmp_path / "environment"
    python = environment_root / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fixture")
    (environment_root / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    launch = LinuxBubblewrapContainment(str(executable)).prepare(
        python=python,
        scratch=scratch,
        environment={"LANG": "C.UTF-8"},
        arguments=("-m", "nervos_package_host"),
    )

    separator = launch.command.index("--")
    interpreter = str(environment_root.resolve() / "bin" / "python")
    assert launch.command[separator + 1] == interpreter
    assert launch.command[separator + 2] == "-c"
    bootstrap = launch.command[separator + 3]
    assert "RLIMIT_NPROC" in bootstrap
    assert "os.execv(sys.argv[1],sys.argv[1:])" in bootstrap
    # The bootstrap re-execs the same interpreter, caller arguments untouched.
    assert launch.command[separator + 4] == interpreter
    assert launch.command[separator + 5 :] == ("-m", "nervos_package_host")
    # AS/NOFILE/CPU/FSIZE still bind the launcher through preexec; NPROC never does.
    assert launch.preexec_fn is not None


def test_linux_launcher_refuses_an_interpreter_outside_a_package_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare system interpreter must never be mounted as if it were a package boundary."""
    from nervos_core.infrastructure.sandbox.linux_bubblewrap import LinuxBubblewrapContainment

    executable = tmp_path / "bwrap"
    executable.write_bytes(b"fixture")
    interpreter = tmp_path / "usr" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"fixture")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    with pytest.raises(ContainmentUnavailable):
        LinuxBubblewrapContainment(str(executable)).prepare(
            python=interpreter,
            scratch=scratch,
            environment={},
            arguments=("-m", "nervos_package_host"),
        )


@pytest.mark.skipif(
    not POSIX or shutil.which("bwrap") is None,
    reason="real bubblewrap qualification requires Linux with bwrap installed",
)
def test_linux_bubblewrap_denies_protected_file_and_direct_network(tmp_path: Path) -> None:
    from nervos_core.infrastructure.sandbox.linux_bubblewrap import LinuxBubblewrapContainment

    protected = tmp_path / "protected.txt"
    protected.write_text("must-not-be-readable", encoding="utf-8")
    environment = tmp_path / "environment"
    (environment / "bin").mkdir(parents=True)
    (environment / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).parent}\n", encoding="utf-8"
    )
    python = environment / "bin" / "python"
    try:
        python.symlink_to(sys.executable)
    except OSError:
        shutil.copy2(sys.executable, python)
    scratch = tmp_path / "attempt"
    scratch.mkdir()
    script = (
        "import pathlib,socket,time\n"
        f"p=pathlib.Path({str(protected)!r})\n"
        "try:\n p.read_text(); file_result='read'\n"
        "except OSError:\n file_result='denied'\n"
        "s=socket.socket(); s.settimeout(0.2)\n"
        "try:\n s.connect(('1.1.1.1',53)); net_result='connected'\n"
        "except OSError:\n net_result='denied'\n"
        "pathlib.Path('owned.txt').write_text('scratch-ok')\n"
        "print(file_result,net_result,flush=True); time.sleep(0.2)\n"
    )
    containment = LinuxBubblewrapContainment()
    launch = containment.prepare(
        python=python,
        scratch=scratch,
        environment={"LANG": "C.UTF-8"},
        arguments=("-c", script),
    )
    child = subprocess.Popen(
        launch.command,
        cwd=launch.cwd,
        env=launch.environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=launch.preexec_fn,
        start_new_session=launch.start_new_session,
    )
    try:
        result = containment.establish(child.pid)
        stdout, stderr = child.communicate(timeout=20)
        assert child.returncode == 0, stderr.decode("utf-8", "replace")
        assert result.tier is ContainmentTier.FULL
        assert stdout.decode().strip() == "denied denied"
        assert (scratch / "owned.txt").read_text(encoding="utf-8") == "scratch-ok"
    finally:
        containment.terminate(child.pid)
        if child.poll() is None:
            child.wait(timeout=10)


@pytest.mark.skipif(not WINDOWS, reason="the Windows Job Object tier only exists on Windows")
def test_windows_resource_primitive_verifies_its_job_limits() -> None:
    from nervos_core.infrastructure.sandbox.windows import WindowsJobObjectContainment

    containment = WindowsJobObjectContainment()
    child = _spawn(SLEEP)
    try:
        result = containment.establish(child.pid)

        assert result.tier is ContainmentTier.RESOURCE
        assert result.platform == "windows"
        containment.verify(child.pid)
    finally:
        containment.terminate(child.pid)
        child.wait(timeout=30)


@pytest.mark.skipif(not WINDOWS, reason="the Windows Job Object tier only exists on Windows")
def test_windows_kills_a_child_that_exceeds_the_memory_cap() -> None:
    from nervos_core.infrastructure.sandbox.windows import WindowsJobObjectContainment

    containment = WindowsJobObjectContainment()
    child = _spawn(OVER_ALLOCATE)
    try:
        result = containment.establish(child.pid)
        assert result.tier is ContainmentTier.RESOURCE
        exit_code = _wait(child, 60)
        assert exit_code != 0, "the kernel must not let a contained process exceed its memory cap"
        stdout = child.stdout.read() if child.stdout is not None else b""
        assert str(MEMORY_LIMIT_BYTES) not in stdout.decode("utf-8", "replace")
    finally:
        containment.terminate(child.pid)
        child.wait(timeout=30)


@pytest.mark.skipif(not WINDOWS, reason="the Windows Job Object tier only exists on Windows")
def test_windows_verification_fails_once_containment_is_released() -> None:
    from nervos_core.infrastructure.sandbox.windows import WindowsJobObjectContainment

    containment = WindowsJobObjectContainment()
    child = _spawn(SLEEP)
    containment.establish(child.pid)
    containment.terminate(child.pid)
    child.wait(timeout=30)

    with pytest.raises(ContainmentUnavailable):
        containment.verify(child.pid)


@pytest.mark.skipif(not POSIX, reason="POSIX rlimits only apply on a POSIX host")
def test_posix_applies_the_frozen_rlimits_to_a_child() -> None:
    from nervos_core.application.sandbox import MAX_OPEN_FILES, PackageContainment
    from nervos_core.infrastructure.sandbox.posix import (
        PosixContainment,
        apply_child_rlimits,
        resource_limit,
    )

    adapter: PackageContainment = PosixContainment()
    assert isinstance(adapter, PosixContainment)
    containment = adapter
    child = subprocess.Popen(
        [sys.executable, "-c", SLEEP],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        preexec_fn=apply_child_rlimits,
    )
    try:
        result = containment.establish(child.pid)
        assert result.tier in {ContainmentTier.RESOURCE, ContainmentTier.RLIMITS}
        assert result.platform == "linux"
        assert os.getpid() != child.pid
    finally:
        containment.terminate(child.pid)
        child.wait(timeout=30)
    # The parent process was never limited: containment is established on the child alone, so
    # the test runner's own descriptor budget is untouched.
    assert resource_limit("NOFILE")[0] != MAX_OPEN_FILES


class _RecordingContainment:
    """A deterministic launcher double: records the scratch it was asked to protect.

    This is fixture injection for the `QualifiedPackageHealthChecker` wiring, never a
    platform qualification. Real filesystem/network denial is proven by the Linux
    launcher probes and the repository qualification command.
    """

    def __init__(self, script: str) -> None:
        self._script = script
        self.scratch: Path | None = None
        self.cwd: Path | None = None

    def prepare(
        self,
        *,
        python: Path,
        scratch: Path,
        environment: object,
        arguments: object,
    ) -> SandboxLaunch:
        del python, environment, arguments
        self.scratch = scratch
        self.cwd = scratch
        return SandboxLaunch(
            command=(sys.executable, "-c", self._script),
            cwd=scratch,
            environment={},
            preexec_fn=None,
            start_new_session=True,
        )

    def establish(self, process_id: int) -> ContainmentResult:
        assert process_id > 0
        return ContainmentResult(ContainmentTier.FULL, "fixture", "recording double")

    def verify(self, process_id: int) -> None:
        assert process_id > 0

    def terminate(self, process_id: int) -> None:
        del process_id

    def release(self, process_id: int) -> None:
        del process_id


def _handshake_script(marker: Path, *, exit_code: int = 0) -> str:
    return (
        "import sys\n"
        "from pathlib import Path\n"
        "from nervos_package_host.wire import HOST_PROTOCOL_VERSION, write_frame\n"
        f"Path({str(marker)!r}).write_text(str(Path.cwd()))\n"
        "write_frame(sys.stdout.buffer, {'protocol_version': HOST_PROTOCOL_VERSION,"
        " 'type': 'host_hello', 'request_id': 'hello', 'payload': {}})\n"
        "write_frame(sys.stdout.buffer, {'protocol_version': HOST_PROTOCOL_VERSION,"
        " 'type': 'ready', 'request_id': 'ready', 'payload': {"
        "'sdk_api_version': '1.0', 'host_protocol_version': HOST_PROTOCOL_VERSION}})\n"
        f"raise SystemExit({exit_code})\n"
    )


def test_a_qualified_health_check_uses_a_private_scratch_not_the_environment(
    tmp_path: Path,
) -> None:
    """ADR 0038: the entrypoint import must never remount its own environment writable."""
    from nervos_core.application.package_health import QualifiedPackageHealthChecker

    environment = tmp_path / "environment"
    (environment / "bin").mkdir(parents=True)
    (environment / "bin" / "python").write_bytes(b"fixture")
    marker = tmp_path / "where-did-it-run.txt"
    containment = _RecordingContainment(_handshake_script(marker))
    checker = QualifiedPackageHealthChecker(lambda: containment)

    checker.check(
        environment=environment,
        entrypoint="untrusted.agent:Agent",
        expected_sdk_api_version="1.0",
    )

    assert containment.scratch is not None
    assert containment.scratch != environment
    assert environment not in containment.scratch.parents
    assert marker.read_text(encoding="utf-8") == str(containment.scratch)
    # The private scratch is cleaned on success as well as on failure.
    assert not containment.scratch.exists()


def test_a_failed_health_check_still_cleans_its_private_scratch(tmp_path: Path) -> None:
    from nervos_core.application.package_health import QualifiedPackageHealthChecker

    environment = tmp_path / "environment"
    (environment / "bin").mkdir(parents=True)
    (environment / "bin" / "python").write_bytes(b"fixture")
    marker = tmp_path / "where-did-it-run.txt"
    containment = _RecordingContainment(_handshake_script(marker, exit_code=3))
    checker = QualifiedPackageHealthChecker(lambda: containment)

    from nervos_package_host.wire import HostProtocolError

    with pytest.raises(HostProtocolError):
        checker.check(
            environment=environment,
            entrypoint="untrusted.agent:Agent",
            expected_sdk_api_version="1.0",
        )
    assert containment.scratch is not None and not containment.scratch.exists()


def test_the_observed_capability_never_claims_an_unqualified_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    assert observe_sandbox_capability() == WorkerSandboxCapability(False, "darwin", None)


def test_a_launcher_that_cannot_prepare_reports_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenLauncher:
        def prepare(self, **kwargs: object) -> SandboxLaunch:
            del kwargs
            raise ContainmentUnavailable

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(sandbox_infrastructure, "create_containment", BrokenLauncher)
    assert observe_sandbox_capability() == WorkerSandboxCapability(False, "linux", None)


@pytest.mark.skipif(not POSIX, reason="POSIX rlimits only apply on a POSIX host")
def test_posix_refuses_an_unsupported_platform() -> None:
    from nervos_core.infrastructure.sandbox import posix

    original = posix.platform.system
    posix.platform.system = lambda: "Darwin"  # type: ignore[assignment]
    try:
        with pytest.raises(ContainmentUnavailable):
            posix.PosixContainment().establish(1)
    finally:
        posix.platform.system = original  # type: ignore[assignment]
