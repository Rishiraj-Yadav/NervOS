"""G1 package identity, SemVer, and strict manifest parser tests.

The parser is the fail-closed boundary for untrusted package metadata: these tests exercise the
byte API, the strict YAML subset, JSON-style scalar semantics, extension handling, declarative
metadata, and the deterministic AgentDefinition projection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import metadata

import pytest
from nervos_core.application.package_manifest import (
    builtin_tool_ids,
    parse_package_manifest,
    project_agent_definition,
)
from nervos_core.domain.packages import (
    NERVOS_CORE_COMPATIBILITY_VERSION,
    SUPPORTED_MANIFEST_VERSION,
    SUPPORTED_RUNTIME_PYTHON,
    InvalidPackageEntrypoint,
    InvalidPackageIdentity,
    InvalidPackageManifest,
    InvalidPackageVersion,
    ManifestVersion,
    PackageIdentity,
    PackageRef,
    PackageVersion,
    validate_entrypoint,
    validate_package_id,
)
from nervos_core.domain.runs import RunLimits
from nervos_core.domain.triggers import TriggerKind

VALID_MANIFEST = """
manifest_version: "1"
package_id: com.acme.invoice
package_name: acme-invoice
package_version: 1.2.3
publisher: Acme Tools
display_name: Acme Invoice Agent
description: Creates invoice reminders.
runtime:
  language: python
  python: ">=3.12,<4"
  entrypoint: acme_invoice.agent:Agent
nervos:
  min_version: "0.1.0"
  max_version: "0.1.0"
configuration:
  schema: config.schema.json
models:
  capabilities:
    - text-generation
tools:
  required:
    - current_time
  optional:
    - calculate
triggers:
  supported:
    - cron
    - webhook
memory:
  reads: true
  writes: false
resources:
  limits:
    max_model_calls: 8
    max_tool_calls: 4
    tool_timeout_ms: 10000
x-nervos-category: finance
"""

UNLIMITED_MANIFEST = VALID_MANIFEST.replace(
    "resources:\n  limits:\n    max_model_calls: 8\n"
    "    max_tool_calls: 4\n    tool_timeout_ms: 10000\n",
    "",
)


def _parse(manifest: str = VALID_MANIFEST):
    return parse_package_manifest(manifest.encode("utf-8"))


def test_package_id_reuses_agent_key_grammar_and_rejects_reserved_namespace() -> None:
    assert validate_package_id("com.acme.1x") == "com.acme.1x"
    for value in ("nervos.chat", "Com.Acme.agent", "com.acme-agent", "com..acme", "com"):
        with pytest.raises(InvalidPackageIdentity):
            validate_package_id(value)


def test_package_version_is_strict_semver_and_orders_by_semver_precedence() -> None:
    ordered = [
        PackageVersion("1.0.0-alpha"),
        PackageVersion("1.0.0-alpha.1"),
        PackageVersion("1.0.0-alpha.beta"),
        PackageVersion("1.0.0-beta"),
        PackageVersion("1.0.0-beta.2"),
        PackageVersion("1.0.0-beta.11"),
        PackageVersion("1.0.0-rc.1"),
        PackageVersion("1.0.0"),
    ]
    assert sorted(reversed(ordered)) == ordered
    assert PackageVersion("1.0.0+build.7") == PackageVersion("1.0.0+build.7")
    for value in ("1", "1.0", "01.0.0", "1.0.0-alpha.01", "1.0.0-"):
        with pytest.raises(InvalidPackageVersion):
            PackageVersion(value)


def test_package_version_is_bounded_for_exact_identity_storage() -> None:
    max_ok = "1.2.3-" + "a" * 50 + "+build"
    assert PackageVersion(max_ok).value == max_ok
    with pytest.raises(InvalidPackageVersion):
        PackageVersion("1.2.3-" + "a" * 70)


def test_build_metadata_is_ignored_for_precedence_but_kept_in_identity() -> None:
    foo = PackageVersion("1.0.0+foo")
    bar = PackageVersion("1.0.0+bar")

    # SemVer precedence excludes build metadata, so neither outranks the other ...
    assert not foo < bar and not bar < foo
    # ... but release identity is the exact version string, so they remain distinct releases and
    # therefore distinct AgentDefinition identities.
    assert foo != bar
    identity = PackageIdentity("com.acme.invoice", foo)
    assert identity.as_agent_definition_id().agent_definition_version == "1.0.0+foo"


def test_manifest_version_and_package_ref_are_distinct_concepts() -> None:
    assert ManifestVersion(SUPPORTED_MANIFEST_VERSION).value == "1"
    with pytest.raises(InvalidPackageManifest):
        ManifestVersion("2")

    pkg_ref = PackageRef("com.acme.invoice", PackageVersion("1.2.3"))
    assert pkg_ref.package_id == "com.acme.invoice"
    assert pkg_ref.package_version.value == "1.2.3"


def test_compatibility_constant_is_synchronized_with_installed_core_version() -> None:
    assert metadata.version("nervos-core") == NERVOS_CORE_COMPATIBILITY_VERSION


@pytest.mark.parametrize(
    "entrypoint",
    [
        "pkg.module",
        "pkg.module:Symbol:Extra",
        "pkg/module:Symbol",
        "..pkg.module:Symbol",
        "pkg..module:Symbol",
        "pkg.module:not-a-symbol",
        "pkg.module:",
        ":Symbol",
    ],
)
def test_entrypoint_syntax_is_validated_without_importing(entrypoint: str) -> None:
    with pytest.raises(InvalidPackageEntrypoint):
        validate_entrypoint(entrypoint)

    assert validate_entrypoint("invoice_reminder.agent:InvoiceReminderAgent") == (
        "invoice_reminder.agent:InvoiceReminderAgent"
    )


def test_manifest_parser_accepts_exact_v1_shape_and_projects_agent_definition() -> None:
    manifest = _parse()

    assert manifest.manifest_version.value == "1"
    assert manifest.package_id == "com.acme.invoice"
    assert manifest.package_version == "1.2.3"
    assert manifest.runtime.language == "python"
    assert manifest.runtime.python == SUPPORTED_RUNTIME_PYTHON
    assert manifest.runtime.entrypoint == "acme_invoice.agent:Agent"
    assert manifest.models.capabilities == ("text-generation",)
    assert manifest.tools.required == ("current_time",)
    assert manifest.tools.optional == ("calculate",)
    assert manifest.triggers.supported == (TriggerKind.CRON, TriggerKind.WEBHOOK)
    assert manifest.memory.reads is True
    assert manifest.memory.writes is False
    assert manifest.nervos.min_version == PackageVersion("0.1.0")
    assert manifest.nervos.max_version == PackageVersion("0.1.0")
    # A top-level lowercase x-nervos-* extension is preserved and inert.
    assert manifest.extensions["x-nervos-category"] == "finance"

    definition = project_agent_definition(manifest)
    assert definition.identity.agent_key == manifest.package_id
    assert definition.identity.agent_definition_version == manifest.package_version
    assert definition.display_name == "Acme Invoice Agent"
    assert definition.limits == RunLimits(
        max_model_calls=8, max_tool_calls=4, tool_timeout_ms=10000
    )


def test_manifest_projects_documented_fixed_v1_run_limits_when_resources_absent() -> None:
    assert project_agent_definition(_parse(UNLIMITED_MANIFEST)).limits == RunLimits()


def test_manifest_parser_rejects_oversized_and_non_utf8_bytes_before_parsing() -> None:
    with pytest.raises(InvalidPackageManifest):
        parse_package_manifest(b"x" * 65_537)
    with pytest.raises(InvalidPackageManifest):
        parse_package_manifest(b'\xff\xfe manifest_version: "1"\n')


def test_manifest_parser_rejects_multi_document_input() -> None:
    with pytest.raises(InvalidPackageManifest):
        _parse(VALID_MANIFEST + "---\nother: value\n")


def test_json_style_scalars_keep_non_json_booleans_and_timestamps_as_text() -> None:
    # `yes`/`on` must not become booleans, so a boolean field rejects them outright.
    with pytest.raises(InvalidPackageManifest):
        _parse(VALID_MANIFEST.replace("reads: true", "reads: yes"))
    with pytest.raises(InvalidPackageManifest):
        _parse(VALID_MANIFEST.replace("writes: false", "writes: on"))
    # A timestamp must stay text, never silently become a date object.
    manifest = _parse(VALID_MANIFEST + "x-nervos-date: 2026-01-01\n")
    assert manifest.extensions["x-nervos-date"] == "2026-01-01"


def test_manifest_parser_rejects_duplicate_keys_aliases_anchors_merge_and_tags() -> None:
    with pytest.raises(InvalidPackageManifest):
        _parse(VALID_MANIFEST + "package_name: duplicate\n")

    alias = VALID_MANIFEST.replace(
        "publisher: Acme Tools", "publisher: &publisher Acme Tools"
    ).replace("display_name: Acme Invoice Agent", "display_name: *publisher")
    with pytest.raises(InvalidPackageManifest):
        _parse(alias)

    merged = VALID_MANIFEST.replace(
        "publisher: Acme Tools", "publisher: Acme Tools\n<<: &base {publisher: Other}"
    ).replace("&base", "")
    with pytest.raises(InvalidPackageManifest):
        _parse(merged)

    tagged = VALID_MANIFEST.replace(
        "package_name: acme-invoice", "package_name: !unsafe acme-invoice"
    )
    with pytest.raises(InvalidPackageManifest):
        _parse(tagged)


def test_unknown_fields_and_non_top_level_extensions_are_rejected() -> None:
    with pytest.raises(InvalidPackageManifest):
        _parse(VALID_MANIFEST + "extra: nope\n")
    # Extensions are top-level only; a nested x-nervos-* is unknown.
    nested = VALID_MANIFEST.replace(
        "tools:\n  required:", "tools:\n  x-nervos-nested: 1\n  required:"
    )
    with pytest.raises(InvalidPackageManifest):
        _parse(nested)
    # The frozen form is lowercase; an uppercase spelling is unknown.
    with pytest.raises(InvalidPackageManifest):
        _parse(VALID_MANIFEST + "X-NERVOS-UPPER: 1\n")


def test_manifest_tool_requirements_must_be_portable_builtin_ids() -> None:
    with pytest.raises(InvalidPackageManifest):
        _parse(VALID_MANIFEST.replace("current_time", "db-primary-key-1"))
    with pytest.raises(InvalidPackageManifest):
        _parse(VALID_MANIFEST.replace("- calculate\n", "- not_a_builtin\n"))


@pytest.mark.parametrize(
    "replacement",
    (
        'manifest_version: "2"\n',
        "package_id: nervos.shadow\n",
        'runtime:\n  language: python\n  python: "3.12"\n  entrypoint: x:y\n',
        'runtime:\n  language: javascript\n  python: ">=3.12,<4"\n  entrypoint: x:y\n',
        "configuration:\n  schema: other.schema.json\n",
        'nervos:\n  min_version: "0.2.0"\n  max_version: "0.2.0"\n',
        'nervos:\n  min_version: "0.1.0"\n  max_version: "0.0.1"\n',
        "resources:\n  limits:\n    max_tool_calls: 99\n",
    ),
)
def test_manifest_parser_fails_closed_for_required_field_violations(replacement: str) -> None:
    key = replacement.split(":", 1)[0]
    lines = VALID_MANIFEST.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(f"{key}:"))
    end = start + 1
    while end < len(lines) and (lines[end].startswith("  ") or lines[end].startswith("    ")):
        end += 1
    candidate = "\n".join((*lines[:start], replacement.rstrip("\n"), *lines[end:]))
    with pytest.raises((InvalidPackageManifest, InvalidPackageIdentity)):
        _parse(candidate)


def test_compatibility_requires_both_bounds_inclusive_and_ordered() -> None:
    missing_max = VALID_MANIFEST.replace('  max_version: "0.1.0"\n', "")
    with pytest.raises(InvalidPackageManifest):
        _parse(missing_max)

    # An inclusive range that excludes the running version fails closed.
    with pytest.raises(InvalidPackageManifest):
        _parse(
            VALID_MANIFEST.replace('min_version: "0.1.0"', 'min_version: "0.2.0"').replace(
                'max_version: "0.1.0"', 'max_version: "0.3.0"'
            )
        )

    accepted = _parse(
        VALID_MANIFEST.replace('min_version: "0.1.0"', 'min_version: "0.1.0"').replace(
            'max_version: "0.1.0"', 'max_version: "0.2.0"'
        )
    )
    assert accepted.nervos.max_version == PackageVersion("0.2.0")


def test_trigger_declarations_are_bounded_to_stage_e_trigger_kinds() -> None:
    with pytest.raises(InvalidPackageManifest):
        _parse(VALID_MANIFEST.replace("    - cron\n    - webhook\n", "    - unsupported\n"))
    with pytest.raises(InvalidPackageManifest):
        _parse(VALID_MANIFEST.replace("    - cron\n    - webhook\n", "    - cron\n    - cron\n"))


def test_memory_declarations_require_real_booleans() -> None:
    with pytest.raises(InvalidPackageManifest):
        _parse(VALID_MANIFEST.replace("reads: true", 'reads: "true"'))


def test_assets_are_inert_relative_paths() -> None:
    manifest = _parse(VALID_MANIFEST + "assets:\n  - assets/logo.png\n  - assets/help/index.html\n")

    assert manifest.assets.paths == ("assets/logo.png", "assets/help/index.html")
    for hostile in ("../escape", "/absolute", "C:evil", "assets\\win", "assets/./x"):
        with pytest.raises(InvalidPackageManifest):
            _parse(VALID_MANIFEST + f"assets:\n  - {hostile!r}\n")


@pytest.mark.parametrize(
    "entrypoint",
    (
        "acme_invoice.agent",  # no `:`
        "acme_invoice.agent:Agent:Extra",  # two `:`
        ":Agent",
        "acme_invoice.agent:",
        "acme_invoice/agent:Agent",
        "acme_invoice..agent:Agent",
        "acme_invoice.9agent:Agent",
        "acme_invoice.agent:9Agent",
    ),
)
def test_entrypoint_must_be_module_path_symbol_without_loading_anything(entrypoint: str) -> None:
    # Quoted so the shape is the only thing under test, not YAML's own plain-scalar rules.
    candidate = VALID_MANIFEST.replace("acme_invoice.agent:Agent", f'"{entrypoint}"')
    with pytest.raises(InvalidPackageEntrypoint):
        _parse(candidate)


def test_builtin_tool_ids_use_the_injected_clock_without_calling_it() -> None:
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return datetime(2026, 1, 2, tzinfo=UTC)

    assert builtin_tool_ids(clock=clock) == ("current_time", "calculate", "json_transform")
    assert calls == 0
