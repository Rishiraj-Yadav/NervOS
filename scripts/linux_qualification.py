"""Repository-owned Linux package-isolation qualification (ADR 0037 / ADR 0038).

Run on a Linux kernel with bubblewrap installed:

    uv run python scripts/linux_qualification.py

The direct probes exercise the production launcher (``create_containment``) on the real
kernel: protected-file denial, direct-network denial, writable private scratch, a
read-only package environment, sibling environments/scratch invisibility, descendant
termination and resource limits. The integrated phase then runs the repository's
deterministic package journey through that same launcher and the transport-bound tests.

``--probes-only`` runs just the direct kernel probes. It is explicitly *not* full
qualification; it exists for minimal containers that do not carry the runtime dependency
set, and the report says so.

Docker or WSL may run this command when the container/kernel exposes user namespaces;
that evidence is Linux-kernel qualification, never native-host acceptance. See
``docs/runtime.md`` for the documented container prerequisites.

Exit codes:
    0  every requested property passed on this host
    1  at least one property failed
    2  this host is not a Linux kernel with bubblewrap (explicit prerequisite skip)

Exit code 2 is a skip, never a pass. Nothing here weakens the production refusal: a host
without a qualified launcher still refuses package execution everywhere else.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nervos_core.application.sandbox import PackageContainment

EXIT_QUALIFIED = 0
EXIT_FAILED = 1
EXIT_SKIPPED = 2

_PROPERTY_NAMES = (
    "protected host file cannot be read",
    "direct outbound connection fails",
    "attempt scratch is writable",
    "package environment is read-only",
    "other scratch/environment is unavailable",
    "spawned descendants die on termination",
    "resource and aggregate-output limits bind",
    "a normal signed package completes through package-host IPC",
    "mediated model and granted MCP tool calls work",
    "an ungranted tool remains denied",
    "context and selected memory reach the SDK",
    "memory proposals follow policy without granting authority",
)

_ISOLATION_TEMPLATE = """\
import json, pathlib, socket
result = {}
try:
    pathlib.Path("__PROTECTED__").read_text()
    result["protected_file"] = "read"
except OSError:
    result["protected_file"] = "denied"
probe = socket.socket()
probe.settimeout(0.3)
try:
    probe.connect(("1.1.1.1", 53))
    result["network"] = "connected"
except OSError:
    result["network"] = "denied"
finally:
    probe.close()
try:
    pathlib.Path("scratch-proof.txt").write_text("scratch-ok")
    result["scratch"] = "writable"
except OSError:
    result["scratch"] = "denied"
try:
    pathlib.Path("__ENVIRONMENT__/qualification-forbidden.txt").write_text("forbidden")
    result["environment"] = "writable"
except OSError:
    result["environment"] = "read-only"
result["other_environment"] = (
    "absent" if not pathlib.Path("__OTHER_ENVIRONMENT__").exists() else "present"
)
result["other_scratch"] = "absent" if not pathlib.Path("__OTHER_SCRATCH__").exists() else "present"
print(json.dumps(result))
"""

_DESCENDANT_TEMPLATE = """\
import subprocess, sys, time
subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import pathlib, time; time.sleep(__DELAY__); "
        "pathlib.Path('__MARKER__').write_text('survived')",
    ]
)
print("spawned", flush=True)
time.sleep(120)
"""

_MEMORY_TEMPLATE = """\
import sys
try:
    value = bytearray(1024 * 1024 * 1024)
except MemoryError:
    print("memory-limited")
    sys.exit(0)
print("allocated", len(value))
sys.exit(3)
"""


@dataclass
class QualificationProperty:
    number: int
    name: str
    status: str = "pending"  # pass / fail / skip
    detail: str = ""


@dataclass
class Report:
    properties: list[QualificationProperty] = field(
        default_factory=lambda: [
            QualificationProperty(number, name)
            for number, name in enumerate(_PROPERTY_NAMES, start=1)
        ]
    )

    def set(self, number: int, status: str, detail: str = "") -> None:
        entry = self.properties[number - 1]
        entry.status = status
        entry.detail = detail

    def render(self, host: str, backend: str) -> None:
        print("NervOS Linux package-isolation qualification")
        print(f"host: {host}")
        print(f"backend: {backend}")
        print()
        for entry in self.properties:
            marker = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP", "pending": "FAIL"}[
                entry.status
            ]
            suffix = f" -- {entry.detail}" if entry.detail else ""
            print(f"[{marker}] {entry.number:>2}. {entry.name}{suffix}")
        passed = sum(1 for entry in self.properties if entry.status == "pass")
        print()
        print(f"RESULT: {passed}/{len(self.properties)} properties passed")


def _environment_skeleton(root: Path, name: str) -> Path:
    """Build the same environment shape the package builder produces on this platform.

    A POSIX ``venv`` installs a symlinked ``bin/python`` plus ``pyvenv.cfg``, so the probe
    uses the same shape: the launcher must mount the environment directory, not whatever the
    interpreter symlink points at.
    """
    environment = root / name
    (environment / "bin").mkdir(parents=True, exist_ok=True)
    base = _base_interpreter()
    (environment / "pyvenv.cfg").write_text(f"home = {base.parent}\n", encoding="utf-8")
    python = environment / "bin" / "python"
    if not python.exists():
        try:
            os.symlink(base, python)
        except OSError:
            shutil.copy2(base, python)
    return environment


_MOUNTED_ROOTS = (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64"), Path("/opt"))


def _base_interpreter() -> Path:
    """The interpreter a package environment really runs.

    A venv's ``bin/python`` symlinks to the *base* interpreter. Inside the sandbox only the
    system roots are mounted, so a probe that symlinked the calling venv's python would
    point at a path that deliberately does not exist in the namespace. Refuse loudly
    instead of silently probing an interpreter the launcher would never grant.
    """
    base = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    if not any(base == root or root in base.parents for root in _MOUNTED_ROOTS):
        raise _UnreachableInterpreter(
            f"base interpreter {base} is outside the mounted system roots"
        )
    return base


class _UnreachableInterpreter(Exception):
    """The probe environment cannot be built from this host's interpreter layout."""


def _run_isolated(
    containment: PackageContainment, python: Path, scratch: Path, script: str, *, timeout: float
) -> subprocess.CompletedProcess[bytes]:
    launch = containment.prepare(
        python=python,
        scratch=scratch,
        environment={"LANG": "C.UTF-8"},
        arguments=("-c", script),
    )
    process = subprocess.Popen(
        launch.command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=launch.cwd,
        env=dict(launch.environment),
        preexec_fn=launch.preexec_fn,
        start_new_session=launch.start_new_session,
    )
    process_id = process.pid
    try:
        containment.establish(process_id)
        containment.verify(process_id)
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        containment.terminate(process_id)
        with contextlib.suppress(Exception):
            process.kill()
        process.wait(timeout=10)
        raise
    finally:
        containment.release(process_id)
    return subprocess.CompletedProcess(launch.command, process.returncode, stdout, stderr)


def _run_probes(containment: PackageContainment, report: Report, root: Path) -> None:
    try:
        environment = _environment_skeleton(root, "environment")
        other_environment = _environment_skeleton(root, "other-environment")
    except _UnreachableInterpreter as error:
        for number in range(1, 8):
            report.set(number, "fail", str(error))
        return
    scratch = root / "attempt-scratch"
    scratch.mkdir()
    other_scratch = root / "other-scratch"
    other_scratch.mkdir()
    protected = root / "protected-sibling.txt"
    protected.write_text("must-not-be-readable", encoding="utf-8")
    python = environment / "bin" / "python"

    script = (
        _ISOLATION_TEMPLATE.replace("__PROTECTED__", str(protected))
        .replace("__ENVIRONMENT__", str(environment))
        .replace("__OTHER_ENVIRONMENT__", str(other_environment))
        .replace("__OTHER_SCRATCH__", str(other_scratch))
    )
    try:
        completed = _run_isolated(containment, python, scratch, script, timeout=30)
    except BaseException as error:  # any launch failure is reported, never raised
        for number in range(1, 6):
            report.set(number, "fail", f"isolation probe could not run: {type(error).__name__}")
        return
    if completed.returncode != 0:
        for number in range(1, 6):
            report.set(number, "fail", "isolation probe exited unsuccessfully")
        return
    try:
        observed = json.loads(completed.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        for number in range(1, 6):
            report.set(number, "fail", "isolation probe produced no readable evidence")
        return

    report.set(
        1,
        "pass" if observed.get("protected_file") == "denied" else "fail",
        f"observed={observed.get('protected_file')!r}",
    )
    report.set(
        2,
        "pass" if observed.get("network") == "denied" else "fail",
        f"observed={observed.get('network')!r}",
    )
    scratch_proof = scratch / "scratch-proof.txt"
    writable = (
        observed.get("scratch") == "writable"
        and scratch_proof.read_text(encoding="utf-8") == "scratch-ok"
    )
    report.set(3, "pass" if writable else "fail", f"observed={observed.get('scratch')!r}")
    report.set(
        4,
        "pass" if observed.get("environment") == "read-only" else "fail",
        f"observed={observed.get('environment')!r}",
    )
    siblings_hidden = (
        observed.get("other_environment") == "absent" and observed.get("other_scratch") == "absent"
    )
    report.set(
        5,
        "pass" if siblings_hidden else "fail",
        f"other_environment={observed.get('other_environment')!r} "
        f"other_scratch={observed.get('other_scratch')!r}",
    )

    _probe_descendants(containment, report, python, scratch)
    _probe_memory_limit(containment, report, python, scratch)


def _probe_descendants(
    containment: PackageContainment, report: Report, python: Path, scratch: Path
) -> None:
    marker = scratch / "descendant-survived.txt"
    delay = 4
    script = _DESCENDANT_TEMPLATE.replace("__DELAY__", str(delay)).replace(
        "__MARKER__", str(marker)
    )
    try:
        launch = containment.prepare(
            python=python,
            scratch=scratch,
            environment={"LANG": "C.UTF-8"},
            arguments=("-c", script),
        )
        process = subprocess.Popen(
            launch.command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=launch.cwd,
            env=dict(launch.environment),
            preexec_fn=launch.preexec_fn,
            start_new_session=launch.start_new_session,
        )
    except BaseException as error:  # any launch failure is reported, never raised
        report.set(6, "fail", f"descendant probe could not start: {type(error).__name__}")
        return
    process_id = process.pid
    try:
        containment.establish(process_id)
        containment.verify(process_id)
        assert process.stdout is not None
        process.stdout.readline()
        time.sleep(0.5)
        containment.terminate(process_id)
        with contextlib.suppress(Exception):
            process.wait(timeout=10)
    finally:
        containment.release(process_id)
        with contextlib.suppress(Exception):
            process.kill()
    time.sleep(delay + 2)
    if marker.exists():
        report.set(6, "fail", "a spawned descendant survived termination")
    else:
        report.set(6, "pass", "descendant marker never appeared")


def _probe_memory_limit(
    containment: PackageContainment, report: Report, python: Path, scratch: Path
) -> None:
    try:
        completed = _run_isolated(containment, python, scratch, _MEMORY_TEMPLATE, timeout=30)
    except BaseException as error:  # any launch failure is reported, never raised
        report.set(7, "fail", f"resource probe could not run: {type(error).__name__}")
        return
    output = completed.stdout.decode("utf-8", "replace").strip()
    if completed.returncode == 0 and output == "memory-limited":
        report.set(7, "pass", "1 GiB allocation refused under the 512 MiB cap")
    else:
        report.set(7, "fail", f"allocation was not limited (output={output!r})")


def _run_pytest_phase(report: Report) -> None:
    command = (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "apps/worker/tests/unit/test_package_transport_bounds.py",
        "apps/worker/tests/integration/test_stage_g5_real_runtime.py"
        "::test_real_package_runtime_runs_under_qualified_linux_isolation",
        "packages/nervos-core/tests/integration/test_runtime_integration.py",
    )
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    output = completed.stdout or completed.stderr
    lines = output.strip().splitlines()
    summary = lines[-1] if lines else "no output"
    if completed.returncode != 0:
        failures = [line.strip() for line in lines if line.startswith(("FAILED", "ERROR"))]
        detail = "; ".join(failures[:3]) or summary
        for number in range(8, 13):
            report.set(number, "fail", f"integrated phase failed: {detail}")
        if report.properties[6].status == "pass":
            report.set(7, "fail", "aggregate-output bound tests failed in the integrated phase")
        return
    memory_probe = report.properties[6]
    if memory_probe.status == "pass":
        report.set(7, "pass", f"{memory_probe.detail}; aggregate-output bound tests passed")
    report.set(8, "pass", "qualified journey: signed install and package-host IPC")
    report.set(9, "pass", "qualified journey: mediated model and granted MCP tool")
    report.set(10, "pass", "qualified journey: revoked grant stays denied")
    report.set(11, "pass", "qualified journey: structured context and selected memory")
    report.set(12, "pass", "runtime-integration policy suite (manual/review/automatic-private)")
    print(f"integrated phase: {summary}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probes-only",
        action="store_true",
        help="run only the direct launcher probes (explicitly not full qualification)",
    )
    arguments = parser.parse_args(argv)

    report = Report()
    host = f"{platform.system()} {platform.release()} ({platform.machine()})"
    if platform.system() != "Linux" or shutil.which("bwrap") is None:
        missing = (
            "not a Linux kernel"
            if platform.system() != "Linux"
            else "bubblewrap (bwrap) is not installed"
        )
        for entry in report.properties:
            entry.status = "skip"
            entry.detail = f"prerequisite missing: {missing}"
        report.render(host, "unavailable")
        print()
        print(f"SKIPPED: {missing}; package execution stays refused on this host.")
        return EXIT_SKIPPED

    from nervos_core.application.sandbox import ContainmentUnavailable
    from nervos_core.infrastructure.sandbox import create_containment

    try:
        containment = create_containment()
    except ContainmentUnavailable:
        for entry in report.properties:
            entry.status = "skip"
            entry.detail = "the production launcher refused this kernel"
        report.render(host, "unavailable")
        print()
        print("SKIPPED: the production launcher refused this kernel; package execution stays")
        print("refused on this host.")
        return EXIT_SKIPPED

    root = Path(tempfile.mkdtemp(prefix="nervos-linux-qualification-"))
    try:
        _run_probes(containment, report, root)
        if arguments.probes_only:
            for number in range(8, 13):
                report.set(number, "skip", "--probes-only: integrated phase not requested")
            if report.properties[6].status == "pass":
                report.set(
                    7,
                    "skip",
                    f"{report.properties[6].detail}; aggregate-output bound tests not requested",
                )
        else:
            _run_pytest_phase(report)
    finally:
        with contextlib.suppress(Exception):
            shutil.rmtree(root, ignore_errors=True)
    report.render(host, "bubblewrap")
    failed = [entry for entry in report.properties if entry.status == "fail"]
    if failed:
        print()
        print("NOT QUALIFIED: " + "; ".join(f"{entry.number}" for entry in failed))
        return EXIT_FAILED
    return EXIT_QUALIFIED


if __name__ == "__main__":
    raise SystemExit(main())
