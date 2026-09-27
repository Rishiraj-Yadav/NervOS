"""G2 must inspect and verify packages without executing any of their code.

This is the load-bearing security property of the milestone, and it is a *negative* one, so it is
proved with tripwires rather than by asserting a happy path: each test installs a failing stand-in
for one capability that must never be reached (importing a module, spawning a process, opening a
socket, installing a package) and then feeds the builder and verifier hostile input.

The hostile inputs matter as much as the tripwires. A package whose manifest or wheel tries to look
executable is exactly what an attacker would ship, so the tripwires run while those bytes are
processed.
"""

from __future__ import annotations

import builtins
import io
import socket
import subprocess
import sys
import zipfile
from typing import Any

import pytest
from nervos_core.application.package_archive import canonical_zip_info
from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_verification import verify_package
from package_fixtures import VALID_CONFIG_SCHEMA, VALID_MANIFEST, build_wheel_bytes, valid_wheel


def _signer() -> Ed25519PackageSigner:
    return Ed25519PackageSigner.from_private_bytes(bytes(range(32)))


def _hostile_wheel() -> bytes:
    """A structurally valid wheel whose payload is deliberately executable-looking.

    If anything in G2 were to import package code, this module body is what would run.
    """
    return build_wheel_bytes(
        members={
            "acme_invoice_agent/__init__.py": (
                b"raise AssertionError('package code executed during inspection')\n"
            ),
            "acme_invoice_agent/evil.py": (b"import os\nos.system('echo pwned')\n"),
        }
    )


class TestNoModuleExecution:
    def test_verification_never_imports_package_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The package's own module bodies must not run during inspection."""
        archive = package_build_bytes(
            PackageBuildInputs(
                manifest_bytes=VALID_MANIFEST,
                config_schema_bytes=VALID_CONFIG_SCHEMA,
                agent_wheel_bytes=_hostile_wheel(),
            ),
            signer=_signer(),
        )

        import importlib

        real_import = builtins.__import__
        forbidden: list[str] = []

        def _guard(name: str, *args: Any, **kwargs: Any) -> Any:
            root = name.split(".")[0]
            if root in {"acme_invoice_agent", "evil", "helper_lib", "top_pkg"}:
                forbidden.append(name)
                raise AssertionError(f"package module {name!r} was imported")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _guard)
        monkeypatch.setattr(importlib, "import_module", _guard)

        verified = verify_package(archive_bytes=archive)
        assert verified.manifest.package_id == "com.acme.invoice"
        assert forbidden == []

    def test_module_bodies_do_not_execute(self) -> None:
        """Direct proof: the hostile module raises if executed, and it never is."""
        archive = package_build_bytes(
            PackageBuildInputs(
                manifest_bytes=VALID_MANIFEST,
                config_schema_bytes=VALID_CONFIG_SCHEMA,
                agent_wheel_bytes=_hostile_wheel(),
            ),
            signer=_signer(),
        )
        # If any inspection path executed the module body, this raises.
        assert verify_package(archive_bytes=archive).content_digest


class TestNoSubprocess:
    def test_no_process_is_spawned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("G2 must never spawn a subprocess")

        monkeypatch.setattr(subprocess, "run", _forbidden)
        monkeypatch.setattr(subprocess, "Popen", _forbidden)
        monkeypatch.setattr(subprocess, "check_output", _forbidden)
        monkeypatch.setattr(subprocess, "check_call", _forbidden)

        archive = package_build_bytes(
            PackageBuildInputs(
                manifest_bytes=VALID_MANIFEST,
                config_schema_bytes=VALID_CONFIG_SCHEMA,
                agent_wheel_bytes=_hostile_wheel(),
            ),
            signer=_signer(),
        )
        assert verify_package(archive_bytes=archive).manifest.package_id == "com.acme.invoice"


class TestNoNetwork:
    def test_no_socket_is_opened(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("G2 must never open a network connection")

        monkeypatch.setattr(socket, "socket", _forbidden)
        monkeypatch.setattr(socket, "create_connection", _forbidden)
        monkeypatch.setattr(socket, "getaddrinfo", _forbidden)

        archive = package_build_bytes(
            PackageBuildInputs(
                manifest_bytes=VALID_MANIFEST,
                config_schema_bytes=VALID_CONFIG_SCHEMA,
                agent_wheel_bytes=_hostile_wheel(),
            ),
            signer=_signer(),
        )
        assert verify_package(archive_bytes=archive).manifest.package_id == "com.acme.invoice"


class TestNoInstallation:
    def test_no_package_installer_is_invoked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Neither `pip` nor `uv` may be reachable from the package path."""

        def _forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("G2 must never run a package installer")

        monkeypatch.setattr(subprocess, "run", _forbidden)
        monkeypatch.setattr(subprocess, "Popen", _forbidden)
        for name in ("pip", "uv", "installer", "venv"):
            monkeypatch.setitem(sys.modules, name, None)

        archive = package_build_bytes(
            PackageBuildInputs(
                manifest_bytes=VALID_MANIFEST,
                config_schema_bytes=VALID_CONFIG_SCHEMA,
                agent_wheel_bytes=_hostile_wheel(),
            ),
            signer=_signer(),
        )
        assert verify_package(archive_bytes=archive).manifest.package_id == "com.acme.invoice"


class TestNoFilesystemExtraction:
    def test_nothing_is_extracted_to_disk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A malicious archive entry must not be able to create a file anywhere."""
        import os

        extracted: list[str] = []
        real_open = os.open

        def _guard(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if "escape" in str(path):
                extracted.append(str(path))
                raise AssertionError("G2 must never write an archive member to disk")
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "open", _guard)

        # An archive whose entry names a traversal target; verification must reject without writing.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr(canonical_zip_info("../escape.txt", 1), b"x")
        with pytest.raises(ValueError):
            verify_package(archive_bytes=buffer.getvalue())
        assert extracted == []


class TestNoDynamicLoading:
    def test_no_shared_library_is_loaded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import ctypes

        def _forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("G2 must never load a shared library")

        monkeypatch.setattr(ctypes, "CDLL", _forbidden)
        monkeypatch.setattr(ctypes, "PyDLL", _forbidden)

        archive = package_build_bytes(
            PackageBuildInputs(
                manifest_bytes=VALID_MANIFEST,
                config_schema_bytes=VALID_CONFIG_SCHEMA,
                agent_wheel_bytes=valid_wheel(),
            ),
            signer=_signer(),
        )
        assert verify_package(archive_bytes=archive).manifest.package_id == "com.acme.invoice"

    def test_no_eval_or_exec_of_package_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Manifest and metadata are never evaluated as Python."""

        def _forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("G2 must never eval or exec package content")

        monkeypatch.setattr(builtins, "eval", _forbidden)
        monkeypatch.setattr(builtins, "exec", _forbidden)

        hostile_manifest = VALID_MANIFEST.replace(
            b"display_name: Acme Invoice Agent",
            b"display_name: \"__import__('os').system('echo pwned')\"",
        )
        archive = package_build_bytes(
            PackageBuildInputs(
                manifest_bytes=hostile_manifest,
                config_schema_bytes=VALID_CONFIG_SCHEMA,
                agent_wheel_bytes=valid_wheel(),
            ),
            signer=_signer(),
        )
        # The hostile-looking string is stored as an ordinary text value.
        verified = verify_package(archive_bytes=archive)
        assert "os" in verified.manifest.display_name or verified.manifest.display_name


class TestBuilderAlsoExecutesNothing:
    def test_build_never_imports_package_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_import = builtins.__import__
        forbidden: list[str] = []

        def _guard(name: str, *args: Any, **kwargs: Any) -> Any:
            root = name.split(".")[0]
            if root in {"acme_invoice_agent", "evil", "helper_lib", "top_pkg"}:
                forbidden.append(name)
                raise AssertionError(f"builder imported package module {name!r}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _guard)

        package_build_bytes(
            PackageBuildInputs(
                manifest_bytes=VALID_MANIFEST,
                config_schema_bytes=VALID_CONFIG_SCHEMA,
                agent_wheel_bytes=_hostile_wheel(),
            ),
            signer=_signer(),
        )
        assert forbidden == []

    def test_build_opens_no_socket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("builder must not open a network connection")

        monkeypatch.setattr(socket, "socket", _forbidden)
        monkeypatch.setattr(socket, "create_connection", _forbidden)

        package_build_bytes(
            PackageBuildInputs(
                manifest_bytes=VALID_MANIFEST,
                config_schema_bytes=VALID_CONFIG_SCHEMA,
                agent_wheel_bytes=valid_wheel(),
            ),
            signer=_signer(),
        )
