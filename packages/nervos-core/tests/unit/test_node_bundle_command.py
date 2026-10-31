# Phase C2: a Node package ships its own runtime in the bundle (ADR 0043 resolution).

from __future__ import annotations

import pytest
from nervos_core.domain.packages import (
    DEFAULT_RUNTIME_KIND,
    PackageRuntime,
)


def test_a_command_of_bin_node_and_app_js_is_a_valid_process_runtime() -> None:
    runtime = PackageRuntime(kind="process", command=("bin/node", "app.js"))
    assert runtime.kind == "process"
    assert runtime.command == ("bin/node", "app.js")
    assert runtime.language == ""
    assert runtime.entrypoint == ""


def test_the_node_style_command_survives_the_parser() -> None:
    from nervos_core.application.package_manifest import parse_package_manifest

    manifest = (
        b'manifest_version: "1"\n'
        b"package_id: com.example.nodeagent\n"
        b"package_name: node-agent\n"
        b"package_version: 1.0.0\n"
        b"publisher: Example\n"
        b"display_name: Node Agent\n"
        b"runtime:\n"
        b"  kind: process\n"
        b"  command:\n"
        b"    - bin/node\n"
        b"    - app.js\n"
        b"nervos:\n"
        b'  min_version: "0.1.0"\n'
        b'  max_version: "0.1.0"\n'
        b"configuration:\n"
        b"  schema: config.schema.json\n"
        b"models:\n"
        b"  capabilities:\n"
        b"    - text-generation\n"
        b"tools:\n"
        b"  required: []\n"
        b"  optional: []\n"
        b"resources:\n"
        b"  limits:\n"
        b"    max_model_calls: 4\n"
        b"    max_tool_calls: 8\n"
        b"    tool_timeout_ms: 10000\n"
    )
    parsed = parse_package_manifest(manifest)
    assert parsed.runtime.kind == "process"
    assert parsed.runtime.command == ("bin/node", "app.js")


def test_command_validation_rules_still_apply_to_a_node_bundle() -> None:
    with pytest.raises(Exception, match="absolute"):
        PackageRuntime(kind="process", command=("C:/node/node.exe",))
    with pytest.raises(Exception, match=r"'..'"):
        PackageRuntime(kind="process", command=("bin/../escape/node", "app.js"))


def test_default_runtime_kind_is_still_python() -> None:
    assert DEFAULT_RUNTIME_KIND == "python"
