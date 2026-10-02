"""Approved admission budgets count every occurrence across agent/dependency wheels."""

from __future__ import annotations

import io
import zipfile

import pytest
from nervos_core.application import package_wheel
from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_verification import verify_package
from nervos_core.application.package_wheel_metadata import (
    MetadataValueLimitExceeded,
    selected_headers,
)
from package_fixtures import (
    TEST_SIGNING_SEED,
    VALID_CONFIG_SCHEMA,
    VALID_MANIFEST,
    build_wheel_bytes,
    valid_wheel,
)


def wheel_with_requirements(values: list[str]) -> bytes:
    output = io.BytesIO()
    metadata = b"Name: pkg\nVersion: 1.0\n" + b"".join(
        b"Requires-Dist: " + value.encode() + b"\n" for value in values
    )
    with (
        zipfile.ZipFile(io.BytesIO(valid_wheel())) as source,
        zipfile.ZipFile(output, "w") as result,
    ):
        for name in source.namelist():
            result.writestr(name, metadata if name.endswith("/METADATA") else source.read(name))
    return output.getvalue()


def padded_requirement(size: int) -> str:
    return "a; extra == 'unselected'".ljust(size)


def test_exact_value_limit_is_accepted_and_next_byte_rejected() -> None:
    exact = padded_requirement(64 * 1024)
    metadata = package_wheel.inspect_wheel(wheel_with_requirements([exact]), filename="agent.whl")
    assert metadata.requires_dist == (exact,)
    package_wheel.validate_dependency_closure({}, extra_requires=[exact])
    with pytest.raises(package_wheel.InvalidDependencyWheel, match="logical value budget"):
        package_wheel.inspect_wheel(wheel_with_requirements([exact + " "]), filename="agent.whl")


def test_exact_aggregate_byte_limit_and_next_byte() -> None:
    exact = padded_requirement(64 * 1024)
    budget = package_wheel.RequirementBudget()
    for _ in range(64):
        budget.consume(exact)
    assert budget.bytes_used == 4 * 1024 * 1024
    with pytest.raises(package_wheel.InvalidDependencyWheel, match="package byte budget"):
        budget.consume("a")


def test_duplicates_and_inactive_markers_count_towards_occurrence_limit() -> None:
    value = "a; extra == 'unselected'"
    metadata = package_wheel.inspect_wheel(
        wheel_with_requirements([value] * 100_000), filename="agent.whl"
    )
    assert len(metadata.requires_dist) == 100_000
    with pytest.raises(package_wheel.InvalidDependencyWheel, match="occurrence budget"):
        package_wheel.inspect_wheel(
            wheel_with_requirements([value] * 100_001), filename="agent.whl"
        )


def test_shared_package_budget_counts_separate_wheels() -> None:
    exact = padded_requirement(64 * 1024)
    budget = package_wheel.RequirementBudget()
    package_wheel.inspect_wheel(
        wheel_with_requirements([exact] * 32), filename="agent.whl", requirement_budget=budget
    )
    package_wheel.inspect_wheel(
        wheel_with_requirements([exact] * 32), filename="agent.whl", requirement_budget=budget
    )
    with pytest.raises(package_wheel.InvalidDependencyWheel, match="package byte budget"):
        package_wheel.inspect_wheel(
            wheel_with_requirements(["a"]), filename="agent.whl", requirement_budget=budget
        )


@pytest.mark.parametrize("budget_kind", ["bytes", "occurrences"])
def test_real_builder_and_verifier_share_budget_across_agent_and_dependency(
    budget_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = "a; extra == 'unselected'"
    inputs = PackageBuildInputs(
        manifest_bytes=VALID_MANIFEST,
        config_schema_bytes=VALID_CONFIG_SCHEMA,
        agent_wheel_bytes=build_wheel_bytes(requires_dist=(value,)),
        dependency_wheels={
            "helper_lib-2.0.0-py3-none-any.whl": build_wheel_bytes(
                name="helper_lib",
                version="2.0.0",
                metadata_name="helper-lib",
                metadata_version="2.0.0",
                requires_dist=(value,),
            )
        },
    )
    signer = Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED)
    signed = package_build_bytes(inputs, signer=signer)
    if budget_kind == "bytes":
        monkeypatch.setattr(package_wheel, "MAX_REQUIREMENT_TOTAL_BYTES", len(value) * 2 - 1)
    else:
        monkeypatch.setattr(package_wheel, "MAX_REQUIREMENT_OCCURRENCES", 1)
    with pytest.raises(package_wheel.InvalidDependencyWheel, match=r"package .* budget exceeded"):
        package_build_bytes(inputs, signer=signer)
    with pytest.raises(package_wheel.InvalidDependencyWheel, match=r"package .* budget exceeded"):
        verify_package(archive_bytes=signed)


def test_stream_rejects_before_retaining_the_rest_of_a_huge_value() -> None:
    consumed = 0

    def chunks():
        nonlocal consumed
        yield b"Requires-Dist: a; "
        for _ in range(1000):
            consumed += 1
            yield b"x" * 65536

    with pytest.raises(MetadataValueLimitExceeded, match="logical value budget"):
        list(
            selected_headers(
                chunks(), frozenset({"requires-dist"}), value_limits={"requires-dist": 65536}
            )
        )
    assert consumed <= 2


def test_ignored_body_and_unknown_fields_have_no_new_limit() -> None:
    raw = b"X:" + b"x" * 100_000 + b"\nRequires-Dist: a; extra == 'x'\n\n" + b"x" * 100_000
    assert list(
        selected_headers([raw], frozenset({"requires-dist"}), value_limits={"requires-dist": 65536})
    ) == [("requires-dist", "a; extra == 'x'")]
