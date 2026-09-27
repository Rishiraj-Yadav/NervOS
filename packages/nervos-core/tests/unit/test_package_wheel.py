"""G2 wheel metadata inspection, dependency lock, and offline closure validation.

Every test here is also a statement about what G2 does *not* do: no test imports a wheel, extracts
one to disk, or contacts a network. The wheel bytes are read as data from start to finish.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from nervos_core.application.package_integrity import canonical_json_bytes, sha256_hex
from nervos_core.application.package_wheel import (
    LOCK_FORMAT_VERSION,
    DependencyClosureError,
    InvalidDependencyLock,
    InvalidDependencyWheel,
    agent_requires_dist,
    build_dependency_lock,
    build_wheelhouse_index,
    inspect_wheel,
    inspect_wheelhouse,
    parse_dependency_lock,
    validate_dependency_closure,
    verify_lock_against_wheelhouse,
)
from package_fixtures import build_wheel_bytes, valid_dependency_wheel, valid_wheel


class TestAgentWheelInspection:
    @pytest.mark.parametrize("create_system", [0, 3])
    def test_valid_wheel_creator_system_is_not_outer_policy(self, create_system: int) -> None:
        metadata = inspect_wheel(
            build_wheel_bytes(create_system=create_system), filename="agent.whl"
        )
        assert metadata.tag == "py3-none-any"

    def test_valid_deflated_wheel_is_accepted(self) -> None:
        metadata = inspect_wheel(
            build_wheel_bytes(compression=zipfile.ZIP_DEFLATED), filename="agent.whl"
        )
        assert metadata.tag == "py3-none-any"

    def test_multiple_identical_tags_are_accepted(self) -> None:
        metadata = inspect_wheel(
            build_wheel_bytes(tags=("py3-none-any", "py3-none-any")), filename="agent.whl"
        )
        assert metadata.tag == "py3-none-any"

    def test_mixed_tags_are_rejected(self) -> None:
        with pytest.raises(InvalidDependencyWheel):
            inspect_wheel(
                build_wheel_bytes(tags=("py3-none-any", "cp312-cp312-win_amd64")),
                filename="agent.whl",
            )

    def test_valid_wheel_is_accepted(self) -> None:
        metadata = inspect_wheel(valid_wheel(), filename="agent.whl")
        assert metadata.tag == "py3-none-any"
        assert metadata.name == "acme-invoice-agent"
        assert metadata.version == "1.2.3"

    def test_digest_and_size_describe_exact_bytes(self) -> None:
        raw = valid_wheel()
        metadata = inspect_wheel(raw, filename="agent.whl")
        assert metadata.sha256 == sha256_hex(raw)
        assert metadata.size == len(raw)

    def test_non_zip_is_rejected(self) -> None:
        with pytest.raises(InvalidDependencyWheel):
            inspect_wheel(b"not a zip", filename="agent.whl")

    def test_non_wheel_extension_is_rejected(self) -> None:
        with pytest.raises(InvalidDependencyWheel):
            inspect_wheel(valid_wheel(), filename="agent.tar.gz")

    def test_wrong_tag_is_rejected(self) -> None:
        raw = build_wheel_bytes(tag="cp312-cp312-win_amd64")
        with pytest.raises(InvalidDependencyWheel):
            inspect_wheel(raw, filename="agent.whl")

    def test_platform_wheel_is_rejected(self) -> None:
        raw = build_wheel_bytes(tag="py3-none-manylinux2014_x86_64")
        with pytest.raises(InvalidDependencyWheel):
            inspect_wheel(raw, filename="agent.whl")

    def test_non_purelib_is_rejected(self) -> None:
        raw = build_wheel_bytes(root_is_purelib="false")
        with pytest.raises(InvalidDependencyWheel):
            inspect_wheel(raw, filename="agent.whl")

    @pytest.mark.parametrize("requires_python", [">=3.13", ">=3.13,<4", "not-a-specifier"])
    def test_incompatible_requires_python_is_rejected(self, requires_python: str) -> None:
        raw = build_wheel_bytes(requires_python=requires_python)
        with pytest.raises(InvalidDependencyWheel):
            inspect_wheel(raw, filename="agent.whl")

    def test_missing_requires_python_is_accepted(self) -> None:
        metadata = inspect_wheel(build_wheel_bytes(requires_python=None), filename="agent.whl")
        assert metadata.requires_python is None

    def test_empty_requires_python_is_rejected(self) -> None:
        with pytest.raises(InvalidDependencyWheel):
            inspect_wheel(build_wheel_bytes(requires_python=""), filename="agent.whl")

    def test_multiple_dist_info_directories_are_rejected(self) -> None:
        raw = build_wheel_bytes(extra_dist_info=True)
        with pytest.raises(InvalidDependencyWheel):
            inspect_wheel(raw, filename="agent.whl")


def _wheel_without(member_suffix: str) -> bytes:
    raw = valid_wheel()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    entries = {
        name: payload for name, payload in entries.items() if not name.endswith(member_suffix)
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


class TestWheelMissingMetadata:
    def test_missing_wheel_file_is_rejected(self) -> None:
        with pytest.raises(InvalidDependencyWheel):
            inspect_wheel(_wheel_without(".dist-info/WHEEL"), filename="agent.whl")

    def test_missing_metadata_is_rejected(self) -> None:
        with pytest.raises(InvalidDependencyWheel):
            inspect_wheel(_wheel_without(".dist-info/METADATA"), filename="agent.whl")

    def test_no_dist_info_at_all_is_rejected(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("pkg/__init__.py", b"")
        with pytest.raises(InvalidDependencyWheel):
            inspect_wheel(buffer.getvalue(), filename="agent.whl")


class TestWheelInternalSafety:
    def test_traversal_inside_a_wheel_is_rejected(self) -> None:
        """A wheel is an archive, so it gets the same path rules the outer archive gets."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("../escape.py", b"x")
            archive.writestr("pkg-1.0.dist-info/METADATA", b"Name: pkg\nVersion: 1.0\n")
            archive.writestr(
                "pkg-1.0.dist-info/WHEEL", b"Tag: py3-none-any\nRoot-Is-Purelib: true\n"
            )
        with pytest.raises(InvalidDependencyWheel):
            inspect_wheel(buffer.getvalue(), filename="agent.whl")


class TestDependencyLock:
    def test_lock_is_derived_from_wheels(self) -> None:
        wheels = {
            "helper_lib-2.0.0-py3-none-any.whl": valid_dependency_wheel(),
        }
        lock = build_dependency_lock(wheels)
        assert len(lock.entries) == 1
        entry = lock.entries[0]
        assert entry.name == "helper-lib"
        assert entry.version == "2.0.0"
        assert entry.wheel == "helper_lib-2.0.0-py3-none-any.whl"
        assert entry.sha256 == sha256_hex(wheels[entry.wheel])

    def test_empty_wheelhouse_gives_empty_lock(self) -> None:
        lock = build_dependency_lock({})
        assert lock.entries == ()
        assert json.loads(lock.canonical_bytes())["dependencies"] == []

    def test_lock_round_trips(self) -> None:
        lock = build_dependency_lock(
            {"helper_lib-2.0.0-py3-none-any.whl": valid_dependency_wheel()}
        )
        assert parse_dependency_lock(lock.canonical_bytes()) == lock

    def test_lock_is_canonical(self) -> None:
        lock = build_dependency_lock(
            {"helper_lib-2.0.0-py3-none-any.whl": valid_dependency_wheel()}
        )
        assert lock.canonical_bytes() == canonical_json_bytes(json.loads(lock.canonical_bytes()))

    def test_lock_digest_is_over_exact_wheel_bytes(self) -> None:
        """The lock pins the artifact it was built from, so different bytes pin differently."""
        original = valid_dependency_wheel("a", "1.0.0")
        altered = build_wheel_bytes(
            name="a",
            version="1.0.0",
            metadata_name="a",
            metadata_version="1.0.0",
            members={"a/extra.py": b"VALUE = 2\n"},
        )
        assert original != altered

        filename = "a-1.0.0-py3-none-any.whl"
        first = build_dependency_lock({filename: original}).entries[0]
        second = build_dependency_lock({filename: altered}).entries[0]
        assert first.sha256 == sha256_hex(original)
        assert second.sha256 == sha256_hex(altered)
        assert first.sha256 != second.sha256
        assert first.size != second.size

    def test_non_canonical_lock_is_rejected(self) -> None:
        lock = build_dependency_lock(
            {"helper_lib-2.0.0-py3-none-any.whl": valid_dependency_wheel()}
        )
        pretty = json.dumps(json.loads(lock.canonical_bytes().decode()), indent=2).encode()
        with pytest.raises(InvalidDependencyLock):
            parse_dependency_lock(pretty)

    def test_unsupported_lock_version_is_rejected(self) -> None:
        with pytest.raises(InvalidDependencyLock):
            parse_dependency_lock(
                canonical_json_bytes({"lock_format_version": "2", "dependencies": []})
            )

    def test_malformed_digest_is_rejected(self) -> None:
        document = {
            "lock_format_version": LOCK_FORMAT_VERSION,
            "dependencies": [
                {
                    "name": "a",
                    "version": "1.0.0",
                    "wheel": "a-1.0.0-py3-none-any.whl",
                    "sha256": "ZZZ",
                    "size": 1,
                }
            ],
        }
        with pytest.raises(InvalidDependencyLock):
            parse_dependency_lock(canonical_json_bytes(document))

    def test_two_versions_of_one_distribution_are_rejected(self) -> None:
        document = {
            "lock_format_version": LOCK_FORMAT_VERSION,
            "dependencies": [
                {
                    "name": "a",
                    "version": "1.0.0",
                    "wheel": "a-1.0.0-py3-none-any.whl",
                    "sha256": "a" * 64,
                    "size": 1,
                },
                {
                    "name": "a",
                    "version": "2.0.0",
                    "wheel": "a-2.0.0-py3-none-any.whl",
                    "sha256": "b" * 64,
                    "size": 1,
                },
            ],
        }
        with pytest.raises(InvalidDependencyLock):
            parse_dependency_lock(canonical_json_bytes(document))


class TestLockWheelhouseConsistency:
    def _pair(self):
        raw = valid_dependency_wheel()
        filename = "helper_lib-2.0.0-py3-none-any.whl"
        lock = build_dependency_lock({filename: raw})
        metadata = inspect_wheel(raw, filename=filename)
        return lock, {filename: metadata}

    def test_matching_lock_and_wheelhouse_verify(self) -> None:
        lock, wheels = self._pair()
        verify_lock_against_wheelhouse(lock, wheels)

    def test_missing_wheel_is_rejected(self) -> None:
        lock, _ = self._pair()
        with pytest.raises(InvalidDependencyLock):
            verify_lock_against_wheelhouse(lock, {})

    def test_extra_wheel_is_rejected(self) -> None:
        """A wheel present in the archive but absent from the lock must not be silently ignored."""
        lock, wheels = self._pair()
        extra_raw = valid_dependency_wheel("other", "3.0.0")
        extra_name = "other-3.0.0-py3-none-any.whl"
        wheels[extra_name] = inspect_wheel(extra_raw, filename=extra_name)
        with pytest.raises(InvalidDependencyLock):
            verify_lock_against_wheelhouse(lock, wheels)

    def test_digest_mismatch_is_rejected(self) -> None:
        lock, wheels = self._pair()
        entry = lock.entries[0]
        tampered = lock.__class__(
            entries=(
                entry.__class__(
                    name=entry.name,
                    version=entry.version,
                    wheel=entry.wheel,
                    sha256="f" * 64,
                    size=entry.size,
                ),
            )
        )
        with pytest.raises(InvalidDependencyLock):
            verify_lock_against_wheelhouse(tampered, wheels)

    def test_version_mismatch_is_rejected(self) -> None:
        lock, wheels = self._pair()
        entry = lock.entries[0]
        tampered = lock.__class__(
            entries=(
                entry.__class__(
                    name=entry.name,
                    version="9.9.9",
                    wheel=entry.wheel,
                    sha256=entry.sha256,
                    size=entry.size,
                ),
            )
        )
        with pytest.raises(InvalidDependencyLock):
            verify_lock_against_wheelhouse(tampered, wheels)

    def test_filename_mismatch_is_rejected(self) -> None:
        lock, wheels = self._pair()
        entry = lock.entries[0]
        tampered = lock.__class__(
            entries=(
                entry.__class__(
                    name=entry.name,
                    version=entry.version,
                    wheel="renamed.whl",
                    sha256=entry.sha256,
                    size=entry.size,
                ),
            )
        )
        with pytest.raises(InvalidDependencyLock):
            verify_lock_against_wheelhouse(tampered, wheels)

    def test_one_artifact_per_distribution_is_enforced(self) -> None:
        indexes = {}
        raw = valid_dependency_wheel("dup", "1.0.0")
        indexes["dup-1.0.0-py3-none-any.whl"] = raw
        with pytest.raises((InvalidDependencyWheel, InvalidDependencyLock)):
            build_wheelhouse_index(
                {
                    "dup-1.0.0-py3-none-any.whl": raw,
                    "renamed-1.0.0-py3-none-any.whl": raw,
                }
            )


class TestOfflineClosureValidation:
    def test_wheelhouse_with_no_requirements_is_complete(self) -> None:
        validate_dependency_closure({"helper_lib-2.0.0-py3-none-any.whl": valid_dependency_wheel()})

    def test_satisfied_dependency_passes(self) -> None:
        """A wheel requiring another distribution present in the wheelhouse is self-contained."""
        dep = valid_dependency_wheel("helper-lib", "2.0.0")
        dep_filename = "helper_lib-2.0.0-py3-none-any.whl"
        top = build_wheel_bytes(
            name="top_pkg",
            version="1.0.0",
            metadata_name="top-pkg",
            metadata_version="1.0.0",
            requires_dist=("helper-lib>=2.0",),
        )
        validate_dependency_closure({dep_filename: dep, "top_pkg-1.0.0-py3-none-any.whl": top})

    def test_unsatisfied_dependency_fails_closed(self) -> None:
        top = build_wheel_bytes(
            name="top_pkg",
            version="1.0.0",
            metadata_name="top-pkg",
            metadata_version="1.0.0",
            requires_dist=("missing-lib>=1.0",),
        )
        with pytest.raises(DependencyClosureError):
            validate_dependency_closure({"top_pkg-1.0.0-py3-none-any.whl": top})

    def test_incompatible_dependency_version_fails_closed(self) -> None:
        top = build_wheel_bytes(
            name="top_pkg",
            version="1.0.0",
            metadata_name="top-pkg",
            metadata_version="1.0.0",
            requires_dist=("helper-lib>=3.0",),
        )
        helper = valid_dependency_wheel("helper-lib", "2.0.0")
        with pytest.raises(DependencyClosureError):
            validate_dependency_closure(
                {
                    "top_pkg-1.0.0-py3-none-any.whl": top,
                    "helper_lib-2.0.0-py3-none-any.whl": helper,
                }
            )

    def test_agent_requirement_version_is_enforced(self) -> None:
        helper = valid_dependency_wheel("helper-lib", "2.0.0")
        with pytest.raises(DependencyClosureError):
            validate_dependency_closure(
                {"helper_lib-2.0.0-py3-none-any.whl": helper},
                extra_requires=("helper-lib>=3.0",),
            )

    @pytest.mark.parametrize(
        "requirement",
        (
            "helper-lib @ https://example.invalid/helper.whl",
            "helper-lib @ file:///tmp/helper.whl",
            'helper-lib @ https://example.invalid/helper.whl ; python_version >= "3.12"',
        ),
    )
    def test_direct_reference_dependency_is_rejected(self, requirement: str) -> None:
        helper = valid_dependency_wheel("helper-lib", "2.0.0")
        with pytest.raises(DependencyClosureError):
            validate_dependency_closure(
                {"helper_lib-2.0.0-py3-none-any.whl": helper},
                extra_requires=(requirement,),
            )

    def test_platform_marker_requirement_is_enforced_for_both_platforms(self) -> None:
        """A Windows-only dependency still has to ship: V1 must install on both platforms."""
        top = build_wheel_bytes(
            name="top_pkg",
            version="1.0.0",
            metadata_name="top-pkg",
            metadata_version="1.0.0",
            requires_dist=('winonly; sys_platform == "win32"',),
        )
        with pytest.raises(DependencyClosureError):
            validate_dependency_closure({"top_pkg-1.0.0-py3-none-any.whl": top})

    def test_inapplicable_marker_requirement_may_be_omitted(self) -> None:
        """A requirement that can never apply is not a closure violation."""
        top = build_wheel_bytes(
            name="top_pkg",
            version="1.0.0",
            metadata_name="top-pkg",
            metadata_version="1.0.0",
            requires_dist=('ancient; python_version < "2.0"',),
        )
        validate_dependency_closure({"top_pkg-1.0.0-py3-none-any.whl": top})

    def test_extra_requirement_is_skipped(self) -> None:
        """V1 ships no extras, and an optional dependency may legitimately be omitted."""
        top = build_wheel_bytes(
            name="top_pkg",
            version="1.0.0",
            metadata_name="top-pkg",
            metadata_version="1.0.0",
            requires_dist=('optional-lib; extra == "dev"',),
        )
        validate_dependency_closure({"top_pkg-1.0.0-py3-none-any.whl": top})

    def test_closure_requires_no_network(self) -> None:
        """Closure is a presence check over shipped artifacts; absence is an error, not a fetch."""
        import socket

        def _forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("closure validation must not open a network connection")

        original = socket.socket
        socket.socket = _forbidden  # type: ignore[assignment]
        try:
            with pytest.raises(DependencyClosureError):
                validate_dependency_closure(
                    {
                        "top_pkg-1.0.0-py3-none-any.whl": build_wheel_bytes(
                            name="top_pkg",
                            version="1.0.0",
                            metadata_name="top-pkg",
                            metadata_version="1.0.0",
                            requires_dist=("missing-lib",),
                        )
                    }
                )
        finally:
            socket.socket = original  # type: ignore[assignment]

    def test_agent_requirements_are_read_from_metadata(self) -> None:
        raw = build_wheel_bytes(requires_dist=("helper-lib>=2.0",))
        assert agent_requires_dist(raw) == ("helper-lib>=2.0",)

    def test_wheelhouse_index_keys_by_distribution_name(self) -> None:
        raw = valid_dependency_wheel("helper-lib", "2.0.0")
        _, by_name = build_wheelhouse_index({"helper_lib-2.0.0-py3-none-any.whl": raw})
        assert "helper-lib" in by_name

    def test_wheelhouse_inspection_is_deterministic(self) -> None:
        wheels = {
            "b_pkg-1.0.0-py3-none-any.whl": valid_dependency_wheel("b-pkg", "1.0.0"),
            "a_pkg-1.0.0-py3-none-any.whl": valid_dependency_wheel("a-pkg", "1.0.0"),
        }
        first = [wheel.name for wheel in inspect_wheelhouse(wheels)]
        second = [wheel.name for wheel in inspect_wheelhouse(dict(reversed(list(wheels.items()))))]
        assert first == second == ["a-pkg", "b-pkg"]
