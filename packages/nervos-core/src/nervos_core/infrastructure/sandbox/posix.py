"""POSIX containment for package-host child processes (ADR 0035).

This module supplies rlimits for explicit resource tests. It provides neither filesystem
nor network isolation, cannot report FULL, and cannot license production package execution.
The production factory refuses all platforms until a combined launcher is qualified.

``resource`` exists only on POSIX, so this module imports it lazily through
:func:`_load_resource`. A Windows checkout type-checks and imports this file without the
POSIX module ever being resolvable; nothing here calls into it unless the platform is
POSIX.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import platform
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Protocol, cast

from nervos_core.application.sandbox import (
    CPU_TIME_SECONDS,
    FILE_SIZE_LIMIT_BYTES,
    MAX_ACTIVE_PROCESSES,
    MAX_OPEN_FILES,
    MEMORY_LIMIT_BYTES,
    ContainmentResult,
    ContainmentTier,
    ContainmentUnavailable,
    SandboxLaunch,
)


class _ResourceModule(Protocol):
    """The slice of the POSIX ``resource`` module this adapter uses."""

    RLIMIT_AS: int
    RLIMIT_CPU: int
    RLIMIT_FSIZE: int
    RLIMIT_NOFILE: int
    RLIMIT_NPROC: int

    def setrlimit(self, resource: int, limits: tuple[int, int]) -> None: ...

    def getrlimit(self, resource: int) -> tuple[int, int]: ...


# ``signal.SIGKILL`` is POSIX-only, so a Windows checkout cannot resolve it; the signal
# number is part of the POSIX ABI and is identical on every platform this adapter runs on.
POSIX_SIGKILL: Final = 9


def _load_resource() -> _ResourceModule:
    """Import the POSIX ``resource`` module, or raise if the platform lacks it."""
    try:
        module = importlib.import_module("resource")
    except ImportError as error:  # pragma: no cover - Windows never reaches containment
        raise ContainmentUnavailable from error
    return cast(_ResourceModule, module)


def unsupported_platform() -> bool:
    return platform.system() not in {"Linux", "Windows"}


def cgroup_v2_writable() -> bool:
    """Whether a writable unified-hierarchy cgroup root can be located."""
    if platform.system() != "Linux":
        return False
    candidates = ("/sys/fs/cgroup",)
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        candidates += (str(Path(runtime_dir) / "cgroup"),)
    for candidate in candidates:
        unified = Path(candidate) / "cgroup.controllers"
        try:
            if unified.is_file() and os.access(str(unified), os.W_OK):
                return True
        except OSError:
            continue
    return False


def resource_limit(name: str) -> tuple[int, int]:
    """Read one POSIX rlimit by its short name (``"AS"``, ``"NOFILE"``, ...).

    Named access keeps this module's own `resource` use behind one typed seam, so a Windows
    checkout -- where the POSIX `resource` module does not exist -- can still import and
    type-check this file, and so tests never have to import a POSIX-only module themselves.
    """
    return _load_resource().getrlimit(getattr(_load_resource(), f"RLIMIT_{name}"))


def apply_child_rlimits() -> None:
    """Set the frozen rlimits in a forked child before exec.

    Called only via ``preexec_fn`` (POSIX) by the Worker at package spawn time. Any
    failure raises so the spawn aborts — a child that cannot be limited must not start.
    """
    limits = _load_resource()
    limits.setrlimit(limits.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    limits.setrlimit(limits.RLIMIT_NOFILE, (MAX_OPEN_FILES, MAX_OPEN_FILES))
    try:
        _, hard = limits.getrlimit(limits.RLIMIT_NPROC)
        # A hard limit of -1 means "unlimited"; never try to raise it.
        ceiling = hard if hard > 0 else MAX_ACTIVE_PROCESSES
        limits.setrlimit(limits.RLIMIT_NPROC, (min(MAX_ACTIVE_PROCESSES, ceiling), hard))
    except (ValueError, OSError):
        # NPROC may be unrlimitable in containerized environments; AS/NOFILE/CPU/FSIZE still bind.
        pass
    limits.setrlimit(limits.RLIMIT_CPU, (CPU_TIME_SECONDS, CPU_TIME_SECONDS))
    limits.setrlimit(limits.RLIMIT_FSIZE, (FILE_SIZE_LIMIT_BYTES, FILE_SIZE_LIMIT_BYTES))


class PosixContainment:
    """Containment evidence for one already-started POSIX child.

    Rlimits apply at spawn in the test launcher. This adapter records that resource tier;
    it does not establish namespaces, network isolation or cgroup containment.
    """

    def __init__(self) -> None:
        self._tracked: dict[int, ContainmentTier] = {}

    def prepare(
        self,
        *,
        python: Path,
        scratch: Path,
        environment: Mapping[str, str],
        arguments: tuple[str, ...],
    ) -> SandboxLaunch:
        return SandboxLaunch(
            command=(str(python), *arguments),
            cwd=scratch,
            environment=environment,
            preexec_fn=apply_child_rlimits,
            start_new_session=True,
        )

    def establish(self, process_id: int) -> ContainmentResult:
        if unsupported_platform():
            raise ContainmentUnavailable
        tier = ContainmentTier.RLIMITS
        self._tracked[process_id] = tier
        details = "rlimits AS/NOFILE/NPROC/CPU/FSIZE; no filesystem/network isolation"
        return ContainmentResult(tier=tier, platform="linux", details=details)

    def verify(self, process_id: int) -> None:
        if process_id not in self._tracked:
            raise ContainmentUnavailable
        try:
            os.kill(process_id, 0)
        except OSError as error:
            raise ContainmentUnavailable from error

    def terminate(self, process_id: int) -> None:
        self.release(process_id)
        # Already gone, or not ours to kill: containment bookkeeping is released either
        # way, and the caller observes the process outcome from the exit status.
        with contextlib.suppress(OSError):
            os.kill(process_id, POSIX_SIGKILL)

    def release(self, process_id: int) -> None:
        self._tracked.pop(process_id, None)
