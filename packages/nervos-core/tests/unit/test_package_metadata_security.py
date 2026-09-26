"""G1 security guards: untrusted package metadata is parsed, never acted upon.

Stage G1 parses attacker-controlled manifest and configuration bytes, so the binding guarantee is a
negative one: this path executes no package code, imports no package module, opens no connection,
creates no directory, touches no database, and grants no authority. Proving a negative needs a
tripwire rather than an assertion about the happy path, so each test installs a failing stand-in for
the one capability that must never be reached and then feeds the parser hostile input.
"""

from __future__ import annotations

import importlib
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from nervos_core.application.package_config_schema import (
    PackageConfigSchemaError,
    parse_config_schema_json,
)
from nervos_core.application.package_manifest import (
    parse_package_manifest,
    project_agent_definition,
)

VALID_MANIFEST = b"""
manifest_version: "1"
package_id: com.acme.invoice
package_name: acme-invoice
package_version: 1.2.3
publisher: Acme Tools
display_name: Acme Invoice Agent
runtime:
  language: python
  python: ">=3.12,<4"
  entrypoint: acme_invoice.agent:Agent
nervos:
  min_version: "0.1.0"
  max_version: "0.2.0"
configuration:
  schema: config.schema.json
tools:
  required: [current_time]
memory:
  reads: true
  writes: false
"""


def _refuse(name: str) -> Any:
    def tripwire(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"{name} must not be reached while parsing package metadata")

    return tripwire


def test_python_object_tags_are_rejected_without_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The classic PyYAML deserialization payload. `SafeLoader` cannot resolve a `python/*` tag, and
    # the loader must report that as a manifest error rather than importing or building anything.
    import os as os_module

    monkeypatch.setattr(os_module, "system", _refuse("os.system"))
    for payload in (
        b"!!python/object/apply:os.system ['echo pwned']\n",
        b"--- !!python/object:os.utsname\n",
        b"manifest_version: !!python/name:os.system\n",
    ):
        with pytest.raises(Exception) as raised:
            parse_package_manifest(payload)
        assert type(raised.value).__name__ in {
            "InvalidPackageManifest",
            "InvalidPackageEntrypoint",
        }, payload


def test_declared_entrypoint_module_is_never_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The entrypoint name is validated as syntax only, so a programmatic import is tripped and the
    # declared name's absence from sys.modules is asserted. G2 owns wheel inspection and G3 owns
    # loading, which is where this name first becomes executable code.
    monkeypatch.setattr(importlib, "import_module", _refuse("importlib.import_module"))
    assert "acme_invoice" not in sys.modules
    manifest = parse_package_manifest(VALID_MANIFEST)
    definition = project_agent_definition(manifest)

    assert manifest.runtime.entrypoint == "acme_invoice.agent:Agent"
    assert definition.identity.agent_key == "com.acme.invoice"
    assert "acme_invoice" not in sys.modules
    assert "acme_invoice.agent" not in sys.modules


def test_parsing_opens_no_network_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "socket", _refuse("socket.socket"))
    monkeypatch.setattr(socket, "create_connection", _refuse("socket.create_connection"))
    monkeypatch.setattr(os, "mkdir", _refuse("os.mkdir"))
    monkeypatch.setattr(os, "makedirs", _refuse("os.makedirs"))
    monkeypatch.setattr(Path, "mkdir", _refuse("Path.mkdir"))
    monkeypatch.setattr(sqlite3, "connect", _refuse("sqlite3.connect"))

    assert parse_package_manifest(VALID_MANIFEST).package_id == "com.acme.invoice"


def test_projection_to_definition_spawns_no_process_and_persists_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = parse_package_manifest(VALID_MANIFEST)
    monkeypatch.setattr(subprocess, "run", _refuse("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", _refuse("subprocess.Popen"))
    monkeypatch.setattr(sqlite3, "connect", _refuse("sqlite3.connect"))

    definition = project_agent_definition(manifest)

    assert definition.identity.agent_key == "com.acme.invoice"
    assert definition.identity.agent_definition_version == "1.2.3"


def test_config_schema_validation_rejects_remote_refs_without_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "socket", _refuse("socket.socket"))
    monkeypatch.setattr(socket, "create_connection", _refuse("socket.create_connection"))

    schema = b'{"type":"object","properties":{"a":{"type":"string"}}}'
    assert parse_config_schema_json(schema) is not None
    # A remote reference is a schema error, never an attempted network retrieval.
    with pytest.raises(PackageConfigSchemaError, match="unsupported JSON Schema keyword"):
        parse_config_schema_json(b'{"$ref":"https://example.com/schema.json"}')


def test_manifest_declarations_carry_no_authority() -> None:
    manifest = parse_package_manifest(VALID_MANIFEST)

    # What a manifest says is kept as inert values. Nothing parsed here is a grant, a row, or a
    # permission check: Stage-D call-time evaluation and Stage-F memory rules are untouched.
    assert manifest.memory.reads is True
    assert manifest.memory.writes is False
    assert manifest.tools.required == ("current_time",)
    assert not hasattr(manifest, "grants")
    assert repr(manifest)
