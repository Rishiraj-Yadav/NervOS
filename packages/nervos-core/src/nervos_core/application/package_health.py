"""Isolated activation health check execution for trusted package environments."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import BinaryIO, cast

from nervos_package_host.wire import HOST_PROTOCOL_VERSION, HostProtocolError, read_frame

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

    def check(
        self,
        *,
        environment: Path,
        entrypoint: str,
        expected_sdk_api_version: str,
    ) -> None:
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        process = subprocess.Popen(
            [
                str(python),
                "-m",
                "nervos_package_host",
                "--health-check",
                "--entrypoint",
                entrypoint,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=environment,
            env=package_host_environment(),
        )
        try:
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
                process.kill()
                process.wait()
