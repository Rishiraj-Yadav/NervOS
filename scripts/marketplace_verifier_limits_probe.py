"""Real negative verifier-limit qualification; only synthetic child workloads."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PYTHON_IMAGE = (
    "python:3.12.12-slim-bookworm@sha256:"
    "2986c55feb36e6cae00fa1fefb454283e4b33f35e75ff8bdd123b134130be301"
)
CHILD = """
import sys, time, subprocess
from nervos_marketplace_service.infrastructure.process_limits import apply_limits
apply_limits(128*1024**2, 1)
mode=sys.argv[1]
print('LIMITS_READY', flush=True)
if mode=='memory':
    try:
        payload=bytearray(512*1024**2)
    except MemoryError:
        sys.exit(2)
    sys.exit(0)
if mode=='cpu':
    while True:
        pass
if mode=='wall':
    time.sleep(100)
if mode=='descendants':
    try:
        child=subprocess.Popen([sys.executable,'-I','-c','pass'])
    except OSError:
        sys.exit(2)
    sys.exit(0 if child.wait(timeout=3)==0 else 2)
"""


def check(command: list[str], mode: str, environment: dict[str, str] | None) -> dict[str, object]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            [*command, mode],
            env=environment,
            capture_output=True,
            text=True,
            timeout=1 if mode == "wall" else 15,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        if mode != "wall":
            raise AssertionError(f"{mode} limit did not terminate the workload") from None
        captured = error.stdout or ""
        if isinstance(captured, bytes):
            captured = captured.decode("ascii", errors="replace")
        if "LIMITS_READY" not in captured:
            raise AssertionError("Wall workload did not reach initialized containment") from None
        return {"mode": mode, "terminated": True, "wall_seconds": time.monotonic() - started}
    if mode == "wall" or result.returncode == 0:
        raise AssertionError(f"{mode} exceeded its allowance without termination")
    # A failure to initialize containment is not evidence that the workload was contained.
    if "Traceback" in result.stderr:
        raise AssertionError(f"{mode} containment initialization/workload failed: {result.stderr}")
    if "LIMITS_READY" not in result.stdout:
        raise AssertionError(f"{mode} workload did not reach initialized containment")
    return {
        "mode": mode,
        "exit": result.returncode,
        "wall_seconds": time.monotonic() - started,
        "terminated": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux", action="store_true")
    args = parser.parse_args()
    environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    with tempfile.TemporaryDirectory(prefix="mp-limits-probe-") as directory:
        environment.update({"TEMP": directory, "TMP": directory})
        if not args.linux:
            results = [
                check([sys.executable, "-I", "-c", CHILD], mode, environment)
                for mode in ("memory", "cpu", "descendants", "wall")
            ]
            print(
                json.dumps({"platform": sys.platform, "passed": True, "results": results}, indent=2)
            )
            return 0
        docker = shutil.which("docker")
        if not docker:
            raise SystemExit("Linux qualification requires Docker")
        source = Path(directory)
        shutil.copy2(
            ROOT
            / "apps/marketplace/src/nervos_marketplace_service/infrastructure/process_limits.py",
            source / "process_limits.py",
        )
        # Only the trusted limit module is copied; no workspace/env/credential mount.
        linux_child = CHILD.replace(
            "from nervos_marketplace_service.infrastructure.process_limits import apply_limits",
            "import runpy; "
            "apply_limits=runpy.run_path('/qualification/process_limits.py')['apply_limits']",
        )
        results: list[dict[str, object]] = []
        for mode in ("memory", "cpu", "descendants", "wall"):
            identifier = "nervos-limits-" + uuid4().hex[:12]
            command = [
                docker,
                "run",
                "--rm",
                "--name",
                identifier,
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--user=65534:65534",
                "--memory=256m",
                "--memory-swap=256m",
                "--pids-limit=4",
                "--mount",
                f"type=bind,source={directory},target=/qualification,readonly",
                PYTHON_IMAGE,
                "python",
                "-I",
                "-c",
                linux_child,
            ]
            try:
                results.append(check(command, mode, None))
            finally:
                # Killing the Docker client alone does not kill its container. Always
                # terminate the named container and wait for removal after wall timeout.
                subprocess.run(
                    [docker, "rm", "-f", identifier], capture_output=True, check=False, timeout=15
                )
        print(
            json.dumps(
                {"platform": "linux", "image": PYTHON_IMAGE, "passed": True, "results": results},
                indent=2,
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
