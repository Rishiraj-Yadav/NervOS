"""Measure valid maximum-pressure G packages; no developer keys or database used."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from nervos_core.application.package_builder import PackageBuildSource, package_build
from nervos_core.application.package_signing import Ed25519PackageSigner


def measured_verifier(
    command: list[str], environment: dict[str, str], root: Path
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    """Observe a dedicated child scratch directory, separate from fixture bytes."""
    scratch = root / "verifier-scratch"
    scratch.mkdir()
    child_environment = {
        **environment,
        "TEMP": str(scratch),
        "TMP": str(scratch),
        "TMPDIR": str(scratch),
    }
    peak = 0
    started = time.monotonic()
    with subprocess.Popen(
        command,
        env=child_environment,
        cwd=scratch,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        while True:
            peak = max(peak, sum(p.stat().st_size for p in scratch.rglob("*") if p.is_file()))
            try:
                stdout, stderr = process.communicate(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - started >= 120:
                    process.kill()
                    process.communicate()
                    raise subprocess.TimeoutExpired(command, 120) from None
        peak = max(peak, sum(p.stat().st_size for p in scratch.rglob("*") if p.is_file()))
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr), {
        "verifier_scratch_peak_observed_bytes": peak,
        "scratch_sampling_interval_seconds": 0.05,
        "scratch_measurement_scope": "private child scratch; excludes read-only input and imports",
    }


def fixture(
    root: Path,
    large: bool,
    metadata_heavy: bool = False,
    maximum_metadata: bool = False,
    header_mib: int = 0,
    header_family: str = "ignored",
) -> Path:
    if header_family == "combined-version":
        large = True
    wheel_tag_mib = header_mib if header_family in ("wheel-tags", "version-and-tags") else 0
    dependency_mib = header_mib if header_family == "wheelhouse-ignored" else 0
    if header_family in ("wheel-tags", "wheelhouse-ignored"):
        header_mib = 0
    manifest = root / "manifest.yaml"
    manifest.write_text(
        """manifest_version: "1"
package_id: com.fixture.resources
package_name: resource-fixture
package_version: 1.0.0
publisher: Synthetic Fixture
display_name: Resource Fixture
runtime:
  language: python
  python: ">=3.12,<4"
  entrypoint: resource_fixture.agent:Agent
nervos:
  min_version: 0.1.0
  max_version: 0.1.0
configuration:
  schema: config.schema.json
assets: [assets/padding.bin]
""",
        encoding="utf-8",
    )
    schema = root / "schema.json"
    schema.write_text('{"type":"object","properties":{},"additionalProperties":false}')
    wheel = root / "agent.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("resource_fixture/agent.py", "raise RuntimeError('NEVER_IMPORT')")
        archive.writestr("resource_fixture/__init__.py", "")
        with archive.open("resource_fixture-1.0.0.dist-info/METADATA", "w") as stream:
            headers = b"Metadata-Version: 2.3\nName: resource-fixture\nVersion: 1.0.0\n"
            if header_family == "long-folded-name":
                headers = b"Metadata-Version: 2.3\nVersion: 1.0.0\nName: resource-fixture\n"
            elif header_family in ("local-version", "combined-version", "version-and-tags"):
                headers = b"Metadata-Version: 2.3\nName: resource-fixture\nVersion: 1.0.0+"
            elif header_family == "release-version":
                headers = b"Metadata-Version: 2.3\nName: resource-fixture\nVersion: "
            elif header_family == "single-local-version":
                headers = b"Metadata-Version: 2.3\nName: resource-fixture\nVersion: 1.0.0+"
            stream.write(headers)
            if header_family == "budget-marker":
                atom = b"extra == 'unselected' or "
                terminal = b"extra == 'unselected'"
                value = b"a; " + atom * ((65536 - 3 - len(terminal)) // len(atom)) + terminal
                value = value.ljust(65536)
                for _ in range(header_mib * 1024**2 // len(value)):
                    stream.write(b"Requires-Dist: " + value + b"\n")
                header_mib = 0
            elif header_family == "budget-count":
                for index in range(100_000):
                    stream.write(f"Requires-Dist: a{index}; extra == 'unselected'\n".encode())
                header_mib = 0
            if header_mib:
                # Unknown metadata fields are accepted by the unchanged G verifier.
                # Keep the required identity fields unique; this stresses header objects,
                # rather than only the already-measured description-body representation.
                if header_family == "requires-python":
                    stream.write(b"Requires-Python: ")
                elif header_family == "large-marker":
                    stream.write(b"Requires-Dist: a; ")
                line = {
                    "ignored": b"X:a\n",
                    "long-folded-name": b" x\n",
                    "requires-python": b">=3.12,\n ",
                    "large-marker": b"extra == 'unselected' or ",
                    "local-version": b"a.",
                    "combined-version": b"a.",
                    "version-and-tags": b"a.",
                    "release-version": b"0.",
                    "single-local-version": b"A",
                }.get(header_family, b"Requires-Dist: a; extra == 'unselected'\n")
                block = line * (65536 // len(line))
                remaining = header_mib * 1024**2
                index = 0
                while remaining >= len(line):
                    if header_family == "unique-requires-dist":
                        chunk = b"".join(
                            f"Requires-Dist: a{number}; extra == 'unselected'\n".encode("ascii")
                            for number in range(index, index + 1024)
                        )
                        if len(chunk) > remaining:
                            break
                        index += 1024
                        stream.write(chunk)
                        remaining -= len(chunk)
                        continue
                    count = min(remaining // len(line), len(block) // len(line))
                    chunk = block[: count * len(line)]
                    stream.write(chunk)
                    remaining -= len(chunk)
                if header_family == "requires-python":
                    stream.write(b">=3.12\n")
                elif header_family == "large-marker":
                    stream.write(b"extra == 'unselected'\n")
                elif header_family in (
                    "local-version",
                    "single-local-version",
                    "combined-version",
                    "version-and-tags",
                ):
                    stream.write(b"a\n")
                elif header_family == "release-version":
                    stream.write(b"0\n")
            stream.write(b"\n")
            if metadata_heavy:
                block = b"Synthetic description body.\n" * 2048
                remaining = (
                    (256 * 1024 * 1024 - len(headers) - 1)
                    if maximum_metadata
                    else 200 * 1024 * 1024
                )
                while remaining:
                    chunk = block[: min(remaining, len(block))]
                    stream.write(chunk)
                    remaining -= len(chunk)
        with archive.open("resource_fixture-1.0.0.dist-info/WHEEL", "w") as stream:
            stream.write(b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            if wheel_tag_mib:
                line = b"Tag: py3-none-any\n"
                remaining = wheel_tag_mib * 1024**2
                block = line * (65536 // len(line))
                while remaining >= len(line):
                    count = min(remaining // len(line), len(block) // len(line))
                    chunk = block[: count * len(line)]
                    stream.write(chunk)
                    remaining -= len(chunk)
        archive.writestr("resource_fixture-1.0.0.dist-info/RECORD", "")
        if large:
            # 1000 MiB declared nested content, streamed while generating the fixture.
            block = b"nested-fixture\n" * 4096
            for index in range(4):
                with archive.open(f"resource_fixture/data{index}.bin", "w") as stream:
                    remaining = (
                        (
                            189
                            if maximum_metadata or header_family == "combined-version"
                            else (190 if metadata_heavy else 250)
                        )
                        * 1024
                        * 1024
                    )
                    while remaining:
                        chunk = block[: min(remaining, len(block))]
                        stream.write(chunk)
                        remaining -= len(chunk)
    padding = root / "padding.bin"
    with padding.open("wb") as stream:
        block = b"resource-padding\n" * 4096
        remaining = (248 * 1024 * 1024) if large else 1024
        while remaining:
            chunk = block[: min(remaining, len(block))]
            stream.write(chunk)
            remaining -= len(chunk)
    output = root / "fixture.nervos"
    dependencies: dict[str, Path] = {}
    if dependency_mib:
        for index in range(4):
            name = f"resource_dep{index}"
            path = root / f"dependency-{index}.whl"
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(f"{name}/__init__.py", "raise RuntimeError('NEVER_IMPORT')")
                directory = f"{name}-1.0.0.dist-info"
                with archive.open(directory + "/METADATA", "w") as stream:
                    stream.write(f"Metadata-Version: 2.3\nName: {name}\nVersion: 1.0.0\n".encode())
                    block = b"X:a\n" * 16384
                    for _ in range(dependency_mib * 16):
                        stream.write(block)
                    stream.write(b"\n")
                archive.writestr(
                    directory + "/WHEEL", b"Root-Is-Purelib: true\nTag: py3-none-any\n"
                )
                archive.writestr(directory + "/RECORD", b"")
            dependencies[f"{name}-1.0.0-py3-none-any.whl"] = path
    package_build(
        PackageBuildSource(
            manifest,
            schema,
            wheel,
            assets={"assets/padding.bin": padding},
            dependency_wheels=dependencies,
        ),
        signer=Ed25519PackageSigner.from_private_bytes(bytes(range(32))),
        destination=output,
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-directory", type=Path)
    parser.add_argument("--header-mib", type=int, default=0)
    parser.add_argument("--header-probe", action="store_true")
    parser.add_argument(
        "--header-family",
        choices=(
            "ignored",
            "requires-dist",
            "unique-requires-dist",
            "long-folded-name",
            "requires-python",
            "large-marker",
            "budget-marker",
            "budget-count",
            "local-version",
            "release-version",
            "single-local-version",
            "combined-version",
            "wheel-tags",
            "version-and-tags",
            "wheelhouse-ignored",
        ),
        default="ignored",
    )
    parser.add_argument("--memory-bytes", type=int, default=2 * 1024**3)
    parser.add_argument("--evidence-file", type=Path)
    parser.add_argument("--linux", action="store_true")
    parser.add_argument("--linux-archive-probe", action="store_true")
    parser.add_argument("--builder-cpu-seconds", type=int, default=240)
    parser.add_argument("--builder-memory-bytes", type=int)
    parser.add_argument("--builder-wall-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.linux:
        return linux_probe(
            args.header_mib or 255,
            args.header_family,
            args.memory_bytes,
            args.evidence_file,
            archive_probe=args.linux_archive_probe,
        )
    if args.build_directory:
        from nervos_marketplace_service.infrastructure.process_limits import (
            apply_limits,
            usage_snapshot,
        )

        apply_limits(args.builder_memory_bytes or args.memory_bytes, args.builder_cpu_seconds)
        exceeded = False
        started = time.monotonic()
        try:
            fixture(
                args.build_directory,
                False,
                header_mib=args.header_mib,
                header_family=args.header_family,
            )
        except MemoryError:
            exceeded = True
        measured: dict[str, object] = {
            "builder_" + key: value for key, value in usage_snapshot().items()
        }
        if exceeded:
            measured["builder_error"] = "verification_resource_exceeded"
        else:
            with zipfile.ZipFile(args.build_directory / "agent.whl") as wheel:
                member = "resource_fixture-1.0.0.dist-info/METADATA"
                measured["metadata_bytes"] = wheel.getinfo(member).file_size
                measured["wheel_metadata_bytes"] = wheel.getinfo(
                    "resource_fixture-1.0.0.dist-info/WHEEL"
                ).file_size
                measured["dependency_wheels"] = len(
                    list(args.build_directory.glob("dependency-*.whl"))
                )
                with wheel.open(member) as content:
                    lines = 0
                    while chunk := content.read(65536):
                        lines += chunk.count(b"\n")
                    measured["header_occurrences"] = lines - 4
        measured["builder_wall_seconds"] = time.monotonic() - started
        print(json.dumps(measured))
        return 2 if exceeded else 0
    if args.header_probe:
        return header_probe(
            args.header_mib or 64,
            args.header_family,
            args.memory_bytes,
            args.evidence_file,
            args.builder_cpu_seconds,
            args.builder_wall_seconds,
            args.builder_memory_bytes,
        )
    results: list[dict[str, object]] = []
    for name, large, metadata_heavy in (
        ("small", False, False),
        ("large", True, False),
        ("metadata-heavy", False, True),
        ("combined-pressure", True, True),
        ("maximum-metadata-pressure", True, True),
    ):
        with tempfile.TemporaryDirectory(prefix="mp-resource-fixture-") as directory:
            root = Path(directory)
            archive = fixture(root, large, metadata_heavy, name == "maximum-metadata-pressure")
            environment = {
                name: os.environ[name] for name in ("SystemRoot", "WINDIR") if name in os.environ
            }
            environment.update({"TEMP": directory, "TMP": directory})
            result, disk = measured_verifier(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-m",
                    "nervos_marketplace_service.verifier_child",
                    str(archive),
                    "--memory-bytes",
                    str(args.memory_bytes),
                    "--cpu-seconds",
                    "120",
                ],
                environment,
                root,
            )
            result.check_returncode()
            measured = json.loads(result.stdout)
            measured.pop("evidence")
            measured.update(disk)
            wall = measured.get("wall_seconds")
            measured.update(
                {
                    "fixture": name,
                    "platform": sys.platform,
                    "verifier_exit": result.returncode,
                    "memory_limit_bytes": args.memory_bytes,
                    "cpu_limit_seconds": 120,
                    "wall_limit_seconds": 120,
                    "within_wall_limit": isinstance(wall, (int, float)) and wall <= 120,
                    "archive_bytes": archive.stat().st_size,
                    "fixture_directory_bytes": sum(
                        p.stat().st_size for p in root.iterdir() if p.is_file()
                    ),
                }
            )
            results.append(measured)
            record_evidence(args.evidence_file, measured)
    print(json.dumps(results, indent=2))
    return 0


def header_probe(
    header_mib: int,
    header_family: str = "ignored",
    memory_bytes: int = 2 * 1024**3,
    evidence_file: Path | None = None,
    builder_cpu_seconds: int = 240,
    builder_wall_seconds: int = 300,
    builder_memory_bytes: int | None = None,
) -> int:
    """Generate through G in a contained builder, then measure its exact valid output."""
    if not 1 <= header_mib <= 255:
        raise ValueError("Fixture metadata must remain within the Stage-G member limit")
    with tempfile.TemporaryDirectory(prefix="mp-header-fixture-") as directory:
        environment = {
            key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ
        }
        environment.update({"TEMP": directory, "TMP": directory})
        build = subprocess.run(
            [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--build-directory",
                directory,
                "--header-mib",
                str(header_mib),
                "--header-family",
                header_family,
                "--memory-bytes",
                str(builder_memory_bytes or memory_bytes),
                "--builder-cpu-seconds",
                str(builder_cpu_seconds),
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=builder_wall_seconds,
            check=False,
        )
        evidence: dict[str, object] = {
            "header_mib": header_mib,
            "input_header_bytes_requested": header_mib * 1024**2,
            "header_family": header_family,
            "platform": sys.platform,
            "memory_limit_bytes": memory_bytes,
            "cpu_limit_seconds": 120,
            "wall_limit_seconds": 120,
            "builder_cpu_limit_seconds": builder_cpu_seconds,
            "builder_memory_limit_bytes": builder_memory_bytes or memory_bytes,
            "builder_wall_limit_seconds": builder_wall_seconds,
            "builder_exit": build.returncode,
        }
        if build.returncode:
            if build.stdout:
                evidence.update(json.loads(build.stdout))
            evidence["builder_stderr"] = build.stderr[-2000:]
            print(json.dumps(evidence, indent=2))
            record_evidence(evidence_file, evidence)
            return 1
        evidence.update(json.loads(build.stdout))
        path = Path(directory) / "fixture.nervos"
        evidence["archive_bytes"] = path.stat().st_size
        with path.open("rb") as signed_archive:
            evidence["archive_sha256"] = hashlib.file_digest(signed_archive, "sha256").hexdigest()
        evidence["fixture_directory_bytes"] = sum(
            p.stat().st_size for p in Path(directory).iterdir()
        )
        command = [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "nervos_marketplace_service.verifier_child",
            str(path),
            "--memory-bytes",
            str(memory_bytes),
            "--cpu-seconds",
            "120",
        ]
        try:
            result, disk = measured_verifier(command, environment, Path(directory))
        except subprocess.TimeoutExpired:
            evidence["verifier_result"] = "wall_limit_terminated"
            evidence["verifier_wall_seconds"] = 120
            evidence["verifier_resources"] = "No final child snapshot after forced termination"
            print(json.dumps(evidence, indent=2))
            record_evidence(evidence_file, evidence)
            return 1
        evidence["verifier_exit"] = result.returncode
        evidence.update(disk)
        if result.stdout:
            measured = json.loads(result.stdout)
            if identity := measured.pop("evidence", None):
                measured["verification_identity"] = {
                    key: identity[key]
                    for key in ("package_id", "exact_version", "archive_sha256", "content_digest")
                }
            evidence.update(measured)
        evidence["verifier_stderr"] = result.stderr[-2000:]
        wall = evidence.get("wall_seconds")
        within_wall = isinstance(wall, (float, int)) and wall <= 120
        evidence["within_wall_limit"] = within_wall
        print(json.dumps(evidence, indent=2))
        record_evidence(evidence_file, evidence)
        return 0 if result.returncode == 0 and within_wall else 1


def record_evidence(path: Path | None, result: dict[str, object]) -> None:
    if path is None:
        return
    from nervos_core.application import package_wheel, package_wheel_metadata, package_wheel_version

    result["parser_source_sha256"] = {
        "package_wheel.py": hashlib.sha256(Path(package_wheel.__file__).read_bytes()).hexdigest(),
        "package_wheel_metadata.py": hashlib.sha256(
            Path(package_wheel_metadata.__file__).read_bytes()
        ).hexdigest(),
        "package_wheel_version.py": hashlib.sha256(
            Path(package_wheel_version.__file__).read_bytes()
        ).hexdigest(),
    }
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    document.setdefault("parser_hardening_measurements", []).append(result)
    # Earlier evidence stays intact. A measurement is not the complete acceptance gate.
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def linux_probe(
    header_mib: int,
    family: str,
    memory: int,
    evidence_file: Path | None,
    *,
    archive_probe: bool = False,
) -> int:
    """Copy trusted source only into a disposable pinned Linux Python image."""
    from importlib.metadata import version

    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Linux qualification requires Docker")
    root = Path(__file__).resolve().parents[1]
    identifier = "nervos-parser-" + uuid4().hex[:12]
    python_image = (
        "python:3.12.12-slim-bookworm@sha256:"
        "2986c55feb36e6cae00fa1fefb454283e4b33f35e75ff8bdd123b134130be301"
    )
    with tempfile.TemporaryDirectory(prefix="mp-linux-parser-") as directory:
        context = Path(directory)
        shutil.copytree(
            root / "packages/nervos-core/src/nervos_core",
            context / "nervos_core",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        package = context / "nervos_marketplace_service"
        (package / "infrastructure").mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "infrastructure/__init__.py").write_text("")
        source = root / "apps/marketplace/src/nervos_marketplace_service"
        for member in ("verifier_child.py", "infrastructure/process_limits.py"):
            shutil.copy2(source / member, package / member)
        shutil.copy2(Path(__file__).resolve(), context / "probe.py")
        (context / "requirements.txt").write_text(
            "\n".join(
                name + "==" + version(name)
                for name in (
                    "packaging",
                    "pyyaml",
                    "cryptography",
                    "cffi",
                    "pycparser",
                    "sqlalchemy",
                    "greenlet",
                )
            )
            + "\n",
            encoding="utf-8",
        )
        (context / "Dockerfile").write_text(
            f"FROM {python_image}\n"
            "COPY requirements.txt /qualification/requirements.txt\n"
            "RUN pip install --no-cache-dir -r /qualification/requirements.txt\n"
            "COPY nervos_core /usr/local/lib/python3.12/site-packages/nervos_core\n"
            "COPY nervos_marketplace_service "
            "/usr/local/lib/python3.12/site-packages/nervos_marketplace_service\n"
            "COPY probe.py /qualification/probe.py\n",
            encoding="utf-8",
        )
        try:
            build = subprocess.run(
                [docker, "build", "--tag", identifier, directory],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                check=False,
            )
            if build.returncode:
                raise RuntimeError("Qualification image build failed: " + build.stderr[-3000:])
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
                "--memory=3g",
                "--memory-swap=3g",
                "--pids-limit=8",
                "--tmpfs",
                "/tmp:rw,size=1g",
                identifier,
                "python",
                "-I",
                "/qualification/probe.py",
                "--memory-bytes",
                str(memory),
            ]
            if not archive_probe:
                command += [
                    "--header-probe",
                    "--header-mib",
                    str(header_mib),
                    "--header-family",
                    family,
                ]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                # Container deadline includes fixture generation plus the unchanged
                # 120-second verifier deadline and bounded process startup/cleanup.
                timeout=425,
                check=False,
            )
            if not result.stdout:
                raise RuntimeError("Linux probe returned no evidence: " + result.stderr[-3000:])
            decoded = json.loads(result.stdout)
            measurements = cast(
                list[dict[str, Any]], decoded if isinstance(decoded, list) else [decoded]
            )
            for evidence in measurements:
                evidence["image"] = python_image
                evidence["identity"] = "uid 65534, capabilities dropped, network disabled"
                record_evidence(evidence_file, evidence)
            print(json.dumps(decoded, indent=2))
            return result.returncode
        finally:
            subprocess.run(
                [docker, "rm", "-f", identifier],
                capture_output=True,
                timeout=15,
                check=False,
            )
            subprocess.run(
                [docker, "image", "rm", identifier],
                capture_output=True,
                timeout=30,
                check=False,
            )


if __name__ == "__main__":
    raise SystemExit(main())
