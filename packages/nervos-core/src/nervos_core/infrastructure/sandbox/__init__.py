"""Platform containment adapters for Stage H4 (ADR 0035, ADR 0038).

The Worker imports :func:`create_containment` at composition time; the selected adapter
is the only OS-primitive location. Nothing in ``nervos-core`` application code imports
this package — the composition root wires it behind the
``nervos_core.application.sandbox.PackageContainment`` protocol.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

from nervos_core.application.sandbox import (
    ContainmentResult,
    ContainmentTier,
    ContainmentUnavailable,
    PackageContainment,
    WorkerSandboxCapability,
)

_PROBE_TIMEOUT_SECONDS = 10.0


def create_containment() -> PackageContainment:
    """Return the platform's containment adapter, or raise on unsupported platforms.

    The refusal is the contract: an unsupported platform never receives a silently
    uncontained adapter. The Worker calls this once per package launch and refuses the
    Run on :class:`ContainmentUnavailable` before any package code starts.
    """
    if platform.system() == "Linux":
        from nervos_core.infrastructure.sandbox.linux_bubblewrap import (
            LinuxBubblewrapContainment,
        )

        return LinuxBubblewrapContainment()
    if platform.system() == "Windows" and os.environ.get(
        "NERVOS_ALLOW_WINDOWS_SANDBOX", ""
    ).lower() in ("1", "true", "yes"):
        from nervos_core.infrastructure.sandbox.windows import (
            WindowsJobObjectContainment,
        )

        return WindowsJobObjectContainment()
    raise ContainmentUnavailable


def observe_sandbox_capability() -> WorkerSandboxCapability:
    """Observe what package sandbox this host can actually establish, right now.

    Presence of a binary is configuration, not qualification. This probe therefore asks
    the production launcher to contain one trivial child and reports the result, so a
    host where namespaces are disabled reports unsupported exactly like a host with no
    ``bwrap`` at all. The result is bounded public copy suitable for a durable Worker row.
    """
    system = ((platform.system() or "unknown").strip().lower() or "unknown")[:32]
    try:
        containment = create_containment()
    except ContainmentUnavailable:
        return WorkerSandboxCapability(False, system, None)
    if not _launcher_probe(containment):
        return WorkerSandboxCapability(False, system, None)
    return WorkerSandboxCapability(True, system, "bubblewrap")


def _launcher_probe(containment: PackageContainment) -> bool:
    """Run one trivial child through the real launcher; never start a package module."""
    if os.name == "nt":  # The qualified launcher only exists on Linux; nothing to probe.
        return False
    with tempfile.TemporaryDirectory(prefix="nervos-sandbox-probe-") as root:
        environment = Path(root) / "environment"
        (environment / "bin").mkdir(parents=True)
        # The launcher mounts a package *environment*; the probe supplies the same shape,
        # including `pyvenv.cfg`, so a symlinked interpreter is treated exactly like one
        # installed by the environment builder.
        (environment / "pyvenv.cfg").write_text(
            f"home = {Path(sys.executable).parent}\n", encoding="utf-8"
        )
        python = environment / "bin" / "python"
        scratch = Path(root) / "scratch"
        scratch.mkdir()
        try:
            os.symlink(sys.executable, python)
        except OSError:
            return False
        try:
            launch = containment.prepare(
                python=python,
                scratch=scratch,
                environment={},
                arguments=("-c", "import sys; sys.exit(0)"),
            )
        except (ContainmentUnavailable, OSError):
            return False
        process = subprocess.Popen(
            launch.command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=launch.cwd,
            env=launch.environment,
            preexec_fn=launch.preexec_fn,
            start_new_session=launch.start_new_session,
        )
        try:
            containment.establish(process.pid)
            containment.verify(process.pid)
        except ContainmentUnavailable:
            _stop(process, containment)
            return False
        try:
            return process.wait(timeout=_PROBE_TIMEOUT_SECONDS) == 0
        except subprocess.TimeoutExpired:
            _stop(process, containment)
            return False
        finally:
            containment.release(process.pid)


def _stop(process: subprocess.Popen[bytes], containment: PackageContainment) -> None:
    containment.terminate(process.pid)
    process.kill()
    process.wait()


__all__ = [
    "ContainmentResult",
    "ContainmentTier",
    "ContainmentUnavailable",
    "PackageContainment",
    "create_containment",
    "observe_sandbox_capability",
]
