"""Differential contract against the former full compat32 Message parser."""

from __future__ import annotations

import email.parser
import io
import random
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping

import pytest
from nervos_core.application import package_wheel
from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_verification import verify_package
from nervos_core.application.package_wheel_metadata import selected_headers
from package_fixtures import (
    TEST_SIGNING_SEED,
    VALID_CONFIG_SCHEMA,
    VALID_MANIFEST,
    valid_dependency_wheel,
    valid_wheel,
)
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

FIELDS = frozenset({"name", "version", "requires-python", "requires-dist"})
CORPUS = (
    b"Name: pkg\nVersion: 1.0\n",
    b"nAmE:\tpkg\r\nVeRsIoN: 1.0\r\n\r\nDescription body\nName: fake\n",
    b"Metadata-Version: 99\nName: pkg\nVersion: 1.0\n",
    b"Name: pkg\nVersion: 1.0\nRequires-Python: >=3.12\n",
    b"Name: pkg\nVersion: 1.0\nRequires-Python: >=3.12\nRequires-Python: <4\n",
    b"Name: pkg\nName: pkg\nVersion: 1.0\n",
    b"Name: pkg\nVersion: 1.0\nVersion: 2.0\n",
    b"Name: pkg\nVersion: 1.0\nRequires-Dist: a\nRequires-Dist: a\n",
    b"Name: pkg\nVersion: 1.0\nRequires-Dist: a; extra == 'x'\n",
    b"Name: pkg\nVersion: 1.0\nRequires-Dist: a; sys_platform == 'win32'\n",
    b"Name: pkg\nVersion: 1.0\nRequires-Dist: a;\n python_version >= '3.12'\n",
    b"Name: pkg\nVersion: 1.0\nRequires-Dist: a\n\t>=1\n",
    b"Name: pkg\nVersion:\n 1.0\n",
    b"Name: pkg\nVersion: 1.0\nDescription: irrelevant\n continuation\n",
    b"Name: pkg\nVersion: 1.0\nUnknown: yes\nName: pkg2\n",
    b"Name: pkg\nVersion: 1.0\n\n",
    b"\tbad first continuation\nName: pkg\nVersion: 1.0\n",
    b"Name: pkg\nVersion: 1.0\nmalformed\nRequires-Dist: hidden\n",
    b"From sender\nName: pkg\nFrom misplaced\nVersion: 1.0\n",
    b"Name: pkg\nVersion: 1.0\nFrom body\n",
    b"Name: pkg\n: missing name\nVersion: 1.0\n",
    b"Name : pkg\nVersion: 1.0\n",
    b"Name: pkg\rVersion: 1.0\rRequires-Dist: a\r\rbody",
    b"Name: pkg\nVersion: 1.0\nDescription: caf\xc3\xa9\n",
    b"Name: pkg\nVersion: 1.0\nRequires-Dist: caf\xc3\xa9\n",
    b"Name: pkg\nVersion: 1.0\nRequires-Dist: =?utf-8?q?a?=\n",
    b"X:" + b"a" * 150_000 + b"\nName: pkg\nVersion: 1.0\n",
    b"Name: pkg\nVersion: 1.0\n\n" + b"body\n" * 30_000,
    b"X" * 150_000 + b": ignored\nName: pkg\nVersion: 1.0\n",
    b"X" * 150_000 + b" invalid\nName: pkg\nVersion: 1.0\n",
)


def old_headers(
    chunks: Iterable[bytes],
    selected: frozenset[str],
    *,
    value_limits: Mapping[str, int] | None = None,
) -> Iterator[tuple[str, str]]:
    del value_limits  # Differential corpus is within the approved policy bounds.
    message = email.parser.BytesParser().parsebytes(b"".join(chunks))
    for name, value in message.items():
        if name.lower() in selected:
            yield name.lower(), value


def comparable(headers: Iterable[tuple[str, str]]) -> list[tuple[str, type, str]]:
    # Non-ASCII bytes produce compat32 Header objects rather than plain strings.
    return [(key, type(value), str(value)) for key, value in headers]


@pytest.mark.parametrize("raw", CORPUS, ids=[f"case-{i}" for i in range(len(CORPUS))])
@pytest.mark.parametrize("chunk_size", [1, 31, 65536])
def test_selected_fields_match_old_message(raw: bytes, chunk_size: int) -> None:
    chunks = (raw[i : i + chunk_size] for i in range(0, len(raw), chunk_size))
    assert comparable(selected_headers(chunks, FIELDS)) == comparable(old_headers([raw], FIELDS))


def replacement_wheel(raw: bytes) -> bytes:
    source = valid_wheel()
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as archive, zipfile.ZipFile(output, "w") as result:
        for name in archive.namelist():
            result.writestr(name, raw if name.endswith("/METADATA") else archive.read(name))
    return output.getvalue()


def outcome(raw: bytes) -> object:
    try:
        return package_wheel.inspect_wheel(raw, filename="agent.whl")
    except Exception as error:
        return type(error), str(error)


@pytest.mark.parametrize("metadata", CORPUS, ids=[f"case-{i}" for i in range(len(CORPUS))])
def test_complete_inspection_matches_old_parser(
    metadata: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = replacement_wheel(metadata)
    actual = outcome(raw)
    monkeypatch.setattr(package_wheel, "selected_headers", old_headers)

    # The old path retained every occurrence, including all single-use duplicates.
    def old_metadata(
        chunks: Iterable[bytes],
        *,
        requirement_budget: package_wheel.RequirementBudget | None = None,
    ) -> dict[str, list[str]]:
        values: dict[str, list[str]] = {}
        for key, value in old_headers(chunks, FIELDS):
            values.setdefault(key, []).append(value)
        return values

    monkeypatch.setattr(package_wheel, "_metadata_headers", old_metadata)
    assert actual == outcome(raw)


def test_existing_wheel_fixtures_match(monkeypatch: pytest.MonkeyPatch) -> None:
    wheels = [valid_wheel(), valid_dependency_wheel()]
    actual = [outcome(raw) for raw in wheels]
    monkeypatch.setattr(package_wheel, "selected_headers", old_headers)
    assert actual == [outcome(raw) for raw in wheels]


def test_generated_malformed_headers_match() -> None:
    rng = random.Random(42)
    for _ in range(400):
        lines = [b"Name: pkg\n", b"Version: 1.0\n"]
        for _ in range(8):
            lines.append(bytes(rng.choices(b"X: \t\r\nabc\x00\xff", k=20)))
            lines.append(b"Requires-Dist: a\n")
        raw = b"".join(lines)
        chunks = (raw[i : i + 7] for i in range(0, len(raw), 7))
        assert comparable(selected_headers(chunks, FIELDS)) == comparable(
            old_headers([raw], FIELDS)
        )


def test_body_is_drained_for_archive_integrity() -> None:
    exhausted = False

    def chunks() -> Iterator[bytes]:
        nonlocal exhausted
        yield b"Name: pkg\nVersion: 1.0\n\n"
        yield b"Name: body\n" * 10_000
        exhausted = True

    assert list(selected_headers(chunks(), FIELDS)) == [("name", "pkg"), ("version", "1.0")]
    assert exhausted


def test_large_folded_chunks_match_original() -> None:
    raw = b"Name: base\n" + b" x\n" * 30_000 + b"Version: 1.0\n"
    chunks = (raw[i : i + 65536] for i in range(0, len(raw), 65536))
    assert comparable(selected_headers(chunks, FIELDS)) == comparable(old_headers([raw], FIELDS))


def test_duplicate_requirements_are_retained() -> None:
    raw = replacement_wheel(b"Name: pkg\nVersion: 1.0\n" + b"Requires-Dist: a\n" * 10_000)
    assert package_wheel.inspect_wheel(raw, filename="agent.whl").requires_dist == ("a",) * 10_000
    assert package_wheel.agent_requires_dist(raw) == ("a",) * 10_000


def test_signed_output_is_identical_to_old_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = PackageBuildInputs(
        manifest_bytes=VALID_MANIFEST,
        config_schema_bytes=VALID_CONFIG_SCHEMA,
        agent_wheel_bytes=valid_wheel(),
        dependency_wheels={"helper_lib-2.0.0-py3-none-any.whl": valid_dependency_wheel()},
    )
    signer = Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED)
    current = package_build_bytes(inputs, signer=signer)
    verified = verify_package(archive_bytes=current)
    monkeypatch.setattr(package_wheel, "selected_headers", old_headers)
    original = package_build_bytes(inputs, signer=signer)
    assert current == original
    assert verified == verify_package(archive_bytes=original)


def test_duplicate_invalid_requirement_still_fails_at_original_position() -> None:
    requirements = ["a; extra == 'unselected'"] * 1000 + ["not a requirement!!!"]
    with pytest.raises(package_wheel.DependencyClosureError):
        package_wheel.validate_dependency_closure({}, extra_requires=requirements)


@pytest.mark.parametrize(
    "requirement",
    [
        "a1; extra == 'unselected'",
        "123; extra == 'unselected'",
        "AbC123 \t; python_version < '3'",
        "a; sys_platform == 'darwin'",
        "a; sys_platform == 'win32'",
        "a; python_version >= '3.12'",
        "a; extra == 'unselected' or sys_platform == 'linux'",
        "a; extra == 'unselected' and sys_platform == 'linux'",
        "a; extra == 'unselected' garbage",
        "a;",
        "a; unknown == 'x'",
        "a-b; extra == 'unselected'",
        "a.b; extra == 'unselected'",
        "a[x]; extra == 'unselected'",
        "a>=1; extra == 'unselected'",
        "a @ https://example.invalid/a; extra == 'unselected'",
        "a;\n extra == 'unselected'",
        "a; extra == 'unselected'\r\n",
        "a; extra == 'unselected' " + " " * 1024,
    ],
)
def test_shared_marker_cache_matches_original_closure(requirement: str) -> None:
    def original() -> None:
        try:
            parsed = Requirement(requirement)
        except InvalidRequirement as error:
            raise package_wheel.DependencyClosureError(
                "wheel declares an unparseable requirement"
            ) from error
        if any(
            parsed.marker is None or parsed.marker.evaluate(environment.as_marker_environment())
            for environment in package_wheel.TARGET_ENVIRONMENTS
        ):
            if parsed.url is not None:
                raise package_wheel.DependencyClosureError(
                    "agent wheel declares a direct-reference dependency; V1 permits offline "
                    "wheelhouse artifacts only"
                )
            name = package_wheel.normalized_distribution_name(parsed.name)
            raise package_wheel.DependencyClosureError(
                f"agent wheel requires dependency {name!r}, which is not in the wheelhouse"
            )

    def result(operation: Callable[[], None]) -> object:
        try:
            operation()
            return None
        except Exception as error:
            return type(error), str(error)

    assert result(original) == result(
        lambda: package_wheel.validate_dependency_closure({}, extra_requires=[requirement])
    )


def test_shared_marker_cache_fallback_after_cache_capacity() -> None:
    requirements = [f"a{i}; extra == 'unselected{i}'" for i in range(140)]
    requirements.extend(f"b{i}; extra == 'unselected'" for i in range(1000))
    assert all(
        not any(
            (marker := Requirement(raw).marker) is None
            or marker.evaluate(environment.as_marker_environment())
            for environment in package_wheel.TARGET_ENVIRONMENTS
        )
        for raw in requirements
    )
    package_wheel.validate_dependency_closure({}, extra_requires=requirements)
    with pytest.raises(package_wheel.DependencyClosureError, match="not in the wheelhouse"):
        package_wheel.validate_dependency_closure({}, extra_requires=[*requirements, "required"])


@pytest.mark.parametrize(
    "suffix", ["", ",", ">=3.13", "<3.12", "garbage", "===3.12", ">=3.13,garbage"]
)
def test_large_requires_python_matches_original_conjunction(suffix: str) -> None:
    value = ">=3.12,\n " * 10_000 + suffix
    try:
        original = "compatible" if Version("3.12") in SpecifierSet(value) else "incompatible"
    except InvalidSpecifier:
        original = "invalid"
    try:
        package_wheel.inspect_wheel(
            replacement_wheel(
                b"Name: pkg\nVersion: 1.0\nRequires-Python: " + value.encode() + b"\n"
            ),
            filename="agent.whl",
        )
        current = "compatible"
    except package_wheel.InvalidDependencyWheel as error:
        current = "invalid" if "valid specifier" in str(error) else "incompatible"
    assert current == original


@pytest.mark.parametrize("kind", ["local", "release", "single-local"])
def test_large_version_inspection_and_signed_bytes_match_original(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = {
        "local": "1.0rc02.post03.dev04+" + "a.0002_B-03." * 10_000 + "z",
        "release": "001.002." * 10_000 + "003rc04.post05.dev06+a",
        "single-local": "1+" + "ABC" * 30_000,
    }[kind]
    raw = replacement_wheel(b"Name: pkg\nVersion: " + value.encode() + b"\n")
    assert package_wheel.inspect_wheel(raw, filename="agent.whl").version == str(Version(value))
    inputs = PackageBuildInputs(
        manifest_bytes=VALID_MANIFEST,
        config_schema_bytes=VALID_CONFIG_SCHEMA,
        agent_wheel_bytes=raw,
    )
    signer = Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED)
    current = package_build_bytes(inputs, signer=signer)
    verified = verify_package(archive_bytes=current)

    def original_normalizer(raw: str) -> str:
        return str(Version(raw))

    monkeypatch.setattr(package_wheel, "normalized_metadata_version", original_normalizer)
    original = package_build_bytes(inputs, signer=signer)
    assert current == original
    assert verified == verify_package(archive_bytes=original)
