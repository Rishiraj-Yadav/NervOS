# Phase D: Linux bridge verification kit.

# These tests are SKIP-UNLESS-Linux-with-bwrap. They are UNVERIFIED until the owner
# runs them on Linux (see docs/linux-verification-runbook.md and
# scripts/verify_linux_bridge.sh). No claim in this file has been executed.

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BWRAP = shutil.which("bwrap")
LINUX_BWRAP = platform.system() == "Linux" and BWRAP is not None

pytestmark = pytest.mark.skipif(
    not LINUX_BWRAP,
    reason="[unverified] requires Linux with bubblewrap; run via scripts/verify_linux_bridge.sh",
)


def _bwrap_base() -> list[str]:
    """The repo's real launcher arguments (linux_bubblewrap.py), minimally mirrored."""
    return [
        "bwrap",
        "--die-with-parent",
        "--unshare-all",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind-try",
        "/lib",
        "/lib",
        "--ro-bind-try",
        "/lib64",
        "/lib64",
        "--ro-bind-try",
        "/bin",
        "/bin",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
    ]


def test_d1_loopback_bind_and_connect_inside_bwrap(tmp_path: Path) -> None:
    """[unverified] 127.0.0.1 bind+connect must work inside --unshare-all."""
    script = (
        "import socket, sys\n"
        "server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "server.bind(('127.0.0.1', 0))\n"
        "server.listen(1)\n"
        "port = server.getsockname()[1]\n"
        "client = socket.create_connection(('127.0.0.1', port), timeout=5)\n"
        "conn, _ = server.accept()\n"
        "client.sendall(b'PING')\n"
        "assert conn.recv(4) == b'PING'\n"
        "print('LOOPBACK_OK')\n"
    )
    result = subprocess.run(
        [*_bwrap_base(), "--tmpfs", "/tmp", "--", sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "LOOPBACK_OK" in result.stdout


def test_d2_outbound_connection_fails_inside_bwrap(tmp_path: Path) -> None:
    """[unverified] An outbound connection must fail inside --unshare-all."""
    script = (
        "import socket, sys\n"
        "try:\n"
        "    socket.create_connection(('10.255.255.1', 80), timeout=3)\n"
        "    print('OUTBOUND_CONNECTED')\n"
        "    sys.exit(1)\n"
        "except OSError:\n"
        "    print('OUTBOUND_BLOCKED')\n"
    )
    result = subprocess.run(
        [*_bwrap_base(), "--tmpfs", "/tmp", "--", sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "OUTBOUND_BLOCKED" in result.stdout
    assert "OUTBOUND_CONNECTED" not in result.stdout


def test_d3_process_package_runs_with_real_bwrap_containment(tmp_path: Path) -> None:
    """[unverified] Real Worker adapter + real LinuxBubblewrapContainment, exec bit from a
    real extracted archive, shim reachable by the child."""
    # The full journey lives in scripts/verify_linux_bridge.sh (it needs the installed
    # package store, a signed archive and the prepared runtime artifacts). This unit
    # asserts the preconditions so a Linux run fails fast with a clear message.
    from nervos_core.infrastructure.sandbox.linux_bubblewrap import LinuxBubblewrapContainment

    bundle = tmp_path / "bundle"
    (bundle / "bin").mkdir(parents=True)
    (bundle / "bin" / "agent").write_text("#!/bin/sh\necho BUNDLE_EXEC_OK\n", encoding="utf-8")
    launch = LinuxBubblewrapContainment().prepare(
        python=Path(sys.executable),
        scratch=tmp_path / "scratch",
        environment={},
        arguments=("-m", "nervos_package_host"),
        package_root=bundle,
    )
    assert any(str(bundle) == item for item in launch.command)
    script = (
        "import os, sys\n"
        "target = sys.argv[1]\n"
        "assert os.access(target, os.X_OK), 'command target not executable'\n"
        "print('EXEC_BIT_OK')\n"
        "try:\n"
        "    open(sys.argv[2] + '/probe.txt', 'w').write('x')\n"
        "    print('BUNDLE_WRITABLE')\n"
        "    sys.exit(1)\n"
        "except OSError:\n"
        "    print('BUNDLE_READONLY_OK')\n"
    )
    result = subprocess.run(
        [
            *launch.command[: launch.command.index("--")],
            "--",
            sys.executable,
            "-c",
            script,
            str(bundle / "bin" / "agent"),
            str(bundle),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "EXEC_BIT_OK" in result.stdout
    assert "BUNDLE_READONLY_OK" in result.stdout


def test_d4_write_to_bundle_fails(tmp_path: Path) -> None:
    """[unverified] A write attempt into the read-only bundle must fail."""
    script = (
        "import sys\n"
        "try:\n"
        "    open(sys.argv[1] + '/probe.txt', 'w').write('x')\n"
        "    print('WRITE_SUCCEEDED')\n"
        "    sys.exit(1)\n"
        "except OSError:\n"
        "    print('WRITE_BLOCKED')\n"
    )
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    result = subprocess.run(
        [
            *_bwrap_base(),
            "--ro-bind",
            str(bundle),
            str(bundle),
            "--",
            sys.executable,
            "-c",
            script,
            str(bundle),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "WRITE_BLOCKED" in result.stdout
    assert "WRITE_SUCCEEDED" not in result.stdout
