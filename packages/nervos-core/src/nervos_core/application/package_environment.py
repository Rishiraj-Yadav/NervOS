"""Content-addressed Python 3.12 package environments built strictly from local wheels."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from dataclasses import dataclass
from pathlib import Path

from nervos_core.application.package_integrity import canonical_json_bytes
from nervos_core.application.package_verification import VerifiedPackage
from nervos_core.application.package_wheel import normalized_distribution_name

HOST_PROTOCOL_VERSION = "1"
_NERVOS_RUNTIME_DISTRIBUTIONS = frozenset({"nervos-sdk", "nervos-package-host"})


class PackageEnvironmentError(ValueError):
    """An immutable package environment could not be prepared or validated."""


@dataclass(frozen=True, slots=True)
class RuntimeWheelArtifact:
    path: Path
    distribution_name: str
    version: str
    sha256: str


@dataclass(frozen=True, slots=True)
class PackageRuntimeArtifacts:
    sdk: RuntimeWheelArtifact
    host: RuntimeWheelArtifact

    def __post_init__(self) -> None:
        if normalized_distribution_name(self.sdk.distribution_name) != "nervos-sdk":
            raise PackageEnvironmentError("runtime SDK artifact must be nervos-sdk")
        if normalized_distribution_name(self.host.distribution_name) != "nervos-package-host":
            raise PackageEnvironmentError("runtime host artifact must be nervos-package-host")
        for artifact in (self.sdk, self.host):
            if not artifact.path.is_file():
                raise PackageEnvironmentError("runtime wheel artifact does not exist")
            if _sha256_file(artifact.path) != artifact.sha256:
                raise PackageEnvironmentError("runtime wheel artifact digest mismatch")


@dataclass(frozen=True, slots=True)
class EnvironmentIdentity:
    canonical_json: str
    digest: str
    relative_key: str


def package_environment_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def environment_identity(
    verified: VerifiedPackage, runtime: PackageRuntimeArtifacts
) -> EnvironmentIdentity:
    if sys.version_info[:2] != (3, 12):
        raise PackageEnvironmentError(
            "G3 environment creation requires the NervOS Python 3.12 runtime"
        )
    supplied_names = {verified.agent_wheel.name, *(wheel.name for wheel in verified.dependencies)}
    collisions = sorted(_NERVOS_RUNTIME_DISTRIBUTIONS.intersection(supplied_names))
    if collisions:
        raise PackageEnvironmentError(
            "package wheelhouse collides with NervOS runtime distributions"
        )
    key = {
        "agent_wheel": {
            "name": verified.agent_wheel.name,
            "sha256": verified.agent_wheel.sha256,
            "version": verified.agent_wheel.version,
        },
        "content_digest": verified.content_digest,
        "dependencies": [
            {"name": item.name, "sha256": item.sha256, "version": item.version}
            for item in sorted(verified.dependencies, key=lambda item: item.name)
        ],
        "dependency_lock_digest": hashlib.sha256(
            verified.dependency_lock.canonical_bytes()
        ).hexdigest(),
        "host": {"sha256": runtime.host.sha256, "version": runtime.host.version},
        "host_protocol_version": HOST_PROTOCOL_VERSION,
        "python": "3.12",
        "sdk": {"sha256": runtime.sdk.sha256, "version": runtime.sdk.version},
    }
    payload = canonical_json_bytes(key)
    digest = hashlib.sha256(payload).hexdigest()
    return EnvironmentIdentity(payload.decode("utf-8"), digest, f"environments/{digest}")


def create_default_runtime_artifacts(root: Path) -> PackageRuntimeArtifacts:
    """Locate or create offline runtime wheel artifacts for nervos-sdk and nervos-package-host."""
    runtime_dir = root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    sdk_path = runtime_dir / "nervos_sdk-0.1.0-py3-none-any.whl"
    host_path = runtime_dir / "nervos_package_host-0.1.0-py3-none-any.whl"

    if not sdk_path.exists():
        sdk_path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    if not host_path.exists():
        host_path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    sdk_sha = _sha256_file(sdk_path)
    host_sha = _sha256_file(host_path)

    return PackageRuntimeArtifacts(
        sdk=RuntimeWheelArtifact(sdk_path, "nervos-sdk", "0.1.0", sdk_sha),
        host=RuntimeWheelArtifact(host_path, "nervos-package-host", "0.1.0", host_sha),
    )


class PackageEnvironmentBuilder:
    """Build exact offline environments without mutating the shared NervOS interpreter."""

    def __init__(self, store_root: Path, runtime: PackageRuntimeArtifacts | None = None) -> None:
        self._root = store_root.expanduser().resolve(strict=False)
        self._runtime = runtime or create_default_runtime_artifacts(self._root)

    @property
    def runtime(self) -> PackageRuntimeArtifacts:
        return self._runtime

    def build(
        self, verified: VerifiedPackage, payload_root: Path
    ) -> tuple[EnvironmentIdentity, Path]:
        identity = environment_identity(verified, self._runtime)
        destination = self._root / identity.relative_key
        if destination.exists():
            self.validate(destination, identity)
            return identity, destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix=f"env-{identity.digest[:12]}-", dir=destination.parent))
        try:
            venv.EnvBuilder(with_pip=True, clear=False, symlinks=False).create(temp)
            python = package_environment_python(temp)
            dependency_paths = [
                payload_root / "dependencies" / "wheels" / wheel.filename
                for wheel in sorted(verified.dependencies, key=lambda item: item.filename)
            ]
            wheels = [
                *dependency_paths,
                payload_root / "agent.whl",
                self._runtime.sdk.path,
                self._runtime.host.path,
            ]
            environment = _pip_environment()
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    "--disable-pip-version-check",
                    "--no-input",
                    *(str(path) for path in wheels),
                ],
                check=True,
                cwd=temp,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            metadata = temp / ".nervos-environment.json"
            metadata.write_text(identity.canonical_json, encoding="utf-8", newline="\n")
            self.validate(temp, identity)
            try:
                os.replace(temp, destination)
            except OSError:
                if not destination.exists():
                    raise
                self.validate(destination, identity)
                shutil.rmtree(temp, ignore_errors=True)
            return identity, destination
        except BaseException:
            shutil.rmtree(temp, ignore_errors=True)
            raise

    def validate(self, root: Path, identity: EnvironmentIdentity) -> None:
        python = package_environment_python(root)
        metadata = root / ".nervos-environment.json"
        if not python.is_file() or not metadata.is_file():
            raise PackageEnvironmentError("package environment is incomplete")
        if metadata.read_text(encoding="utf-8") != identity.canonical_json:
            raise PackageEnvironmentError("package environment identity mismatch")
        result = subprocess.run(
            [
                str(python),
                "-c",
                "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=_child_python_environment(),
        )
        if result.stdout.strip() != "3.12":
            raise PackageEnvironmentError("package environment interpreter is not Python 3.12")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _platform_environment() -> dict[str, str]:
    names = (
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
    return {name: os.environ[name] for name in names if name in os.environ}


def _child_python_environment() -> dict[str, str]:
    environment = _platform_environment()
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
        }
    )
    return environment


def _pip_environment() -> dict[str, str]:
    environment = _child_python_environment()
    environment.update(
        {
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
        }
    )
    return environment
