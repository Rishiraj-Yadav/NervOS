"""Linux bubblewrap launcher providing pre-exec filesystem and network isolation."""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
from collections.abc import Mapping
from pathlib import Path

from nervos_core.application.sandbox import (
    MAX_ACTIVE_PROCESSES,
    ContainmentResult,
    ContainmentTier,
    ContainmentUnavailable,
    SandboxLaunch,
)
from nervos_core.infrastructure.sandbox.posix import POSIX_SIGKILL, apply_launcher_rlimits

# ``RLIMIT_NPROC`` cannot bind the launcher itself: bubblewrap creates its namespaces
# by forking, and the kernel refuses that fork with ``EAGAIN`` once this user already
# has ``MAX_ACTIVE_PROCESSES`` processes anywhere on the host (reproduced on Ubuntu
# 24.04 with a 42-process user; AS/NOFILE/CPU/FSIZE do not break the same launch). The
# frozen budget still has to bind package code, so the launcher execs the interpreter
# through a bootstrap that applies the process budget to itself and then ``exec``s the
# real program — the interpreter keeps the limit across that final exec, while the
# namespace bootstrap in front of it runs without the process budget attached.
_PROCESS_BUDGET_BOOTSTRAP = (
    "import os,resource,sys\n"
    "try:\n"
    "    _,hard=resource.getrlimit(resource.RLIMIT_NPROC)\n"
    f"    ceiling=hard if hard>0 else {MAX_ACTIVE_PROCESSES}\n"
    f"    resource.setrlimit(resource.RLIMIT_NPROC,(min({MAX_ACTIVE_PROCESSES},ceiling),hard))\n"
    "except (ValueError,OSError):\n"
    "    pass\n"
    "os.execv(sys.argv[1],sys.argv[1:])\n"
)


class LinuxBubblewrapContainment:
    """Launch a package in mount, user, pid and network namespaces before Python starts."""

    def __init__(self, executable: str | None = None) -> None:
        resolved = executable or shutil.which("bwrap")
        if platform.system() != "Linux" or not resolved:
            raise ContainmentUnavailable
        self._executable = str(Path(resolved).resolve(strict=True))
        self._tracked: set[int] = set()

    def prepare(
        self,
        *,
        python: Path,
        scratch: Path,
        environment: Mapping[str, str],
        arguments: tuple[str, ...],
    ) -> SandboxLaunch:
        # Do not resolve the interpreter symlink. A POSIX virtual environment's ``bin/python``
        # points at the system interpreter, while the directory that must be mounted is the
        # environment root. Mount the canonical root and execute the interpreter through its
        # in-environment path so the venv's own site-packages load inside the namespace.
        python = Path(os.path.abspath(python))
        package_environment = Path(os.path.realpath(python.parent.parent))
        environment_python = package_environment / python.parent.name / python.name
        if not environment_python.is_symlink() and not environment_python.is_file():
            raise ContainmentUnavailable
        # A real package environment is a virtual environment; anything else (for example a
        # system interpreter, whose parent chain would mount ``/usr``) is refused rather than
        # mounted as if it were a package boundary.
        if not (package_environment / "pyvenv.cfg").is_file():
            raise ContainmentUnavailable
        scratch = Path(os.path.realpath(scratch))
        if not scratch.is_dir():
            raise ContainmentUnavailable
        command: list[str] = [
            self._executable,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--cap-drop",
            "ALL",
            "--clearenv",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
        ]
        # Read-only system/runtime roots only. The user home is deliberately absent, so a
        # Python installed under it (for example a per-user interpreter) cannot be reached and
        # package execution refuses rather than widening the mount surface.
        system_roots = tuple(
            root
            for root in (
                Path("/usr"),
                Path("/usr/local"),
                Path("/opt"),
                Path("/lib"),
                Path("/lib64"),
                Path("/bin"),
            )
            if root.exists()
        )
        for root in system_roots:
            command.extend(("--ro-bind", str(root), str(root)))
        # A copied interpreter carries no RPATH to the prefix it was copied from, so the
        # dynamic loader needs its own cache. Only those loader files are mounted: never
        # /etc as a whole, which would carry host configuration and credentials.
        loader_entries = [
            path
            for path in (
                Path("/etc/ld.so.cache"),
                Path("/etc/ld.so.conf"),
                Path("/etc/ld.so.conf.d"),
            )
            if path.exists()
        ]
        if loader_entries:
            command.extend(("--dir", "/etc"))
            for path in loader_entries:
                command.extend(("--ro-bind", str(path), str(path)))
        for directory in _missing_mount_parents(package_environment, scratch, system_roots):
            command.extend(("--dir", str(directory)))
        command.extend(("--ro-bind", str(package_environment), str(package_environment)))
        command.extend(("--bind", str(scratch), str(scratch), "--chdir", str(scratch)))
        safe_environment = {
            "HOME": str(scratch),
            "TMP": str(scratch),
            "TEMP": str(scratch),
            "TMPDIR": str(scratch),
            "PATH": f"{environment_python.parent}:/usr/local/bin:/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
        }
        for name in ("LANG", "LC_ALL"):
            if value := environment.get(name):
                safe_environment[name] = value
        library_path = _environment_library_path(package_environment)
        if library_path is not None:
            safe_environment["LD_LIBRARY_PATH"] = str(library_path)
        for name, value in safe_environment.items():
            command.extend(("--setenv", name, value))
        # argv after ``--`` is [interpreter, "-c", bootstrap, interpreter, *arguments]:
        # the bootstrap applies RLIMIT_NPROC and re-execs the same interpreter, so the
        # program the caller asked for finally sees exactly the arguments it expected.
        command.extend(
            (
                "--",
                str(environment_python),
                "-c",
                _PROCESS_BUDGET_BOOTSTRAP,
                str(environment_python),
                *arguments,
            )
        )
        return SandboxLaunch(
            command=tuple(command),
            cwd=scratch,
            environment={},
            preexec_fn=apply_launcher_rlimits,
            start_new_session=True,
        )

    def establish(self, process_id: int) -> ContainmentResult:
        if process_id <= 0:
            raise ContainmentUnavailable
        self._tracked.add(process_id)
        self.verify(process_id)
        return ContainmentResult(
            tier=ContainmentTier.FULL,
            platform="linux",
            details="bubblewrap filesystem/network namespaces plus POSIX rlimits",
        )

    def verify(self, process_id: int) -> None:
        if process_id not in self._tracked:
            raise ContainmentUnavailable
        try:
            os.kill(process_id, 0)
        except OSError as error:
            raise ContainmentUnavailable from error

    def terminate(self, process_id: int) -> None:
        self.release(process_id)
        with contextlib.suppress(OSError):
            os.kill(-process_id, POSIX_SIGKILL)

    def release(self, process_id: int) -> None:
        self._tracked.discard(process_id)


def _environment_library_path(environment: Path) -> Path | None:
    """The base prefix library directory named by the environment's own ``pyvenv.cfg``.

    A copied interpreter cannot find ``libpython`` on its own, so the loader is pointed at the
    library directory of the prefix the environment declares. The value comes from the frozen
    package environment rather than from any caller-supplied variable, and it is only used
    when that directory actually exists.
    """
    try:
        lines = (environment / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or key.strip() != "home":
            continue
        candidate = Path(value.strip()).parent / "lib"
        return candidate if candidate.is_dir() else None
    return None


def _missing_mount_parents(
    environment: Path, scratch: Path, system_roots: tuple[Path, ...]
) -> tuple[Path, ...]:
    """Create namespace mount parents without exposing their host contents."""
    parents: set[Path] = set()
    for target in (environment, scratch):
        current = target.parent
        while current != current.parent and not any(
            current == root or root in current.parents for root in system_roots
        ):
            parents.add(current)
            current = current.parent
    return tuple(sorted(parents, key=lambda item: (len(item.parts), str(item))))
