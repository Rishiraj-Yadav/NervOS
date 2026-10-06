"""Isolated activation health check execution for trusted package environments."""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import BinaryIO, cast

from nervos_package_host.wire import HOST_PROTOCOL_VERSION, HostProtocolError, read_frame

from nervos_core.application.sandbox import ContainmentUnavailable, PackageContainment

_HOST_START_TIMEOUT = 10.0


def package_host_environment() -> dict[str, str]:
    allowed = (
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
    )
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment.update({"PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"})
    return environment


class SubprocessPackageHealthChecker:
    """Validate package environment and entrypoint asynchronously before activation."""

    def __init__(self, *, authorize_launch: Callable[[], None] | None = None) -> None:
        # Explicit injection exists for trusted deterministic fixtures, never an
        # environment/configuration bypass. Production has no qualified launcher yet.
        self._authorize_launch = authorize_launch

    def _containment(self) -> PackageContainment:
        # Production composition replaces this method with the qualified factory through
        # ``QualifiedPackageHealthChecker``. The base remains a fail-closed test seam.
        raise ContainmentUnavailable

    def check(
        self,
        *,
        environment: Path,
        entrypoint: str,
        expected_sdk_api_version: str,
    ) -> None:
        arguments = (
            "-m",
            "nervos_package_host",
            "--health-check",
            "--entrypoint",
            entrypoint,
        )
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if self._authorize_launch is not None:
            # Explicit trusted-fixture seam. Never reachable through production composition.
            self._authorize_launch()
            process = _spawn(
                command=(str(python), *arguments),
                cwd=environment,
                environment=package_host_environment(),
                preexec_fn=None,
                start_new_session=False,
            )
            _finish_health_check(process, expected_sdk_api_version, containment=None)
            return
        containment = self._containment()
        # ADR 0038: the entrypoint import is package code, so it runs through the same
        # pre-exec launcher as normal execution -- with its own private scratch. Passing
        # the package environment as scratch would remount the immutable environment
        # writable and let an import mutate what it was installed as.
        with tempfile.TemporaryDirectory(prefix="nervos-health-check-") as scratch_directory:
            launch = containment.prepare(
                python=python,
                scratch=Path(scratch_directory),
                environment=package_host_environment(),
                arguments=arguments,
            )
            process = _spawn(
                command=launch.command,
                cwd=launch.cwd,
                environment=launch.environment,
                preexec_fn=launch.preexec_fn,
                start_new_session=launch.start_new_session,
            )
            _finish_health_check(process, expected_sdk_api_version, containment=containment)


def _spawn(
    *,
    command: tuple[str, ...],
    cwd: Path,
    environment: Mapping[str, str],
    preexec_fn: Callable[[], None] | None,
    start_new_session: bool,
) -> subprocess.Popen[bytes]:
    """Start the health-check host. Stderr is discarded so a noisy import cannot deadlock."""
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=cwd,
        env=dict(environment),
        preexec_fn=preexec_fn,
        start_new_session=start_new_session,
    )


def _finish_health_check(
    process: subprocess.Popen[bytes],
    expected_sdk_api_version: str,
    *,
    containment: PackageContainment | None,
) -> None:
    """Prove the handshake, then terminate the contained tree on every outcome."""
    try:
        if containment is not None:
            try:
                containment.establish(process.pid)
            except ContainmentUnavailable:
                _kill(process)
                raise
            containment.verify(process.pid)
        assert process.stdout is not None
        hello = read_frame(cast(BinaryIO, process.stdout))
        ready = read_frame(cast(BinaryIO, process.stdout))
        if hello["type"] != "host_hello" or ready["type"] != "ready":
            raise HostProtocolError("health check handshake failed")
        payload_object = ready["payload"]
        if not isinstance(payload_object, dict):
            raise HostProtocolError("health check payload is malformed")
        payload = cast("dict[str, object]", payload_object)
        if payload.get("sdk_api_version") != expected_sdk_api_version:
            raise HostProtocolError("health check SDK version mismatch")
        if payload.get("host_protocol_version") != HOST_PROTOCOL_VERSION:
            raise HostProtocolError("health check protocol mismatch")
        if process.wait(timeout=_HOST_START_TIMEOUT) != 0:
            raise HostProtocolError("health check host exited unsuccessfully")
    finally:
        if process.poll() is None:
            if containment is not None:
                containment.terminate(process.pid)
            _kill(process)
        if containment is not None:
            containment.release(process.pid)


def _kill(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(Exception):
        process.kill()
        process.wait()


class QualifiedPackageHealthChecker(SubprocessPackageHealthChecker):
    """Production checker using the same qualified containment as Worker execution."""

    def __init__(self, containment_factory: Callable[[], PackageContainment]) -> None:
        super().__init__()
        self._containment_factory = containment_factory

    def _containment(self) -> PackageContainment:
        return self._containment_factory()

    def validate_platform(self) -> None:
        """Refuse unavailable containment before materializing an environment."""
        self._containment_factory()
