"""G2 reproducibility: the frozen claim that the same inputs give the same output.

The claim under test is narrow and deliberate (Stage-G §26): payload wheels and assets are hashed as
exact bytes, NervOS-generated metadata is canonicalized, and archive metadata is fixed. G2 does not
promise to build a wheel from source, so the inputs here are pre-built.

Both halves of the guarantee are asserted: an identical `content_digest` *and* a byte-identical
archive. Only the first would let container framing drift, and only the second would let the
container hide a change in what was signed.
"""

from __future__ import annotations

import pytest
from nervos_core.application.package_builder import (
    PackageBuildInputs,
    package_build_bytes,
)
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_verification import verify_package
from package_fixtures import (
    OTHER_TEST_SIGNING_SEED,
    TEST_SIGNING_SEED,
    VALID_CONFIG_SCHEMA,
    VALID_MANIFEST,
    build_wheel_bytes,
    valid_dependency_wheel,
    valid_wheel,
)


def _signer(seed: bytes = TEST_SIGNING_SEED) -> Ed25519PackageSigner:
    return Ed25519PackageSigner.from_private_bytes(seed)


def _inputs(**overrides: object) -> PackageBuildInputs:
    base: dict[str, object] = {
        "manifest_bytes": VALID_MANIFEST,
        "config_schema_bytes": VALID_CONFIG_SCHEMA,
        "agent_wheel_bytes": valid_wheel(),
    }
    base.update(overrides)
    return PackageBuildInputs(**base)  # type: ignore[arg-type]


def _rich_inputs() -> PackageBuildInputs:
    """Inputs exercising every payload class at once, so reproducibility covers all of them."""
    return _inputs(
        readme_bytes=b"# Acme Invoice\n",
        dependency_wheels={
            "helper_lib-2.0.0-py3-none-any.whl": valid_dependency_wheel("helper-lib", "2.0.0"),
            "other_lib-3.1.0-py3-none-any.whl": valid_dependency_wheel("other-lib", "3.1.0"),
        },
        manifest_bytes=VALID_MANIFEST.replace(
            b"configuration:\n  schema: config.schema.json\n",
            b"configuration:\n  schema: config.schema.json\nassets:\n  - assets/logo.png\n",
        ),
        assets={"assets/logo.png": b"\x89PNG\r\n\x1a\n"},
    )


class TestReproducibleBuild:
    def test_two_builds_are_byte_identical(self) -> None:
        first = package_build_bytes(_rich_inputs(), signer=_signer())
        second = package_build_bytes(_rich_inputs(), signer=_signer())
        assert first == second

    def test_two_builds_share_a_content_digest(self) -> None:
        first = verify_package(archive_bytes=package_build_bytes(_rich_inputs(), signer=_signer()))
        second = verify_package(archive_bytes=package_build_bytes(_rich_inputs(), signer=_signer()))
        assert first.content_digest == second.content_digest

    def test_two_builds_share_an_archive_digest(self) -> None:
        first = verify_package(archive_bytes=package_build_bytes(_rich_inputs(), signer=_signer()))
        second = verify_package(archive_bytes=package_build_bytes(_rich_inputs(), signer=_signer()))
        assert first.archive_digest == second.archive_digest

    def test_build_is_independent_of_mapping_order(self) -> None:
        """Assets and wheels come from mappings, whose order must not reach the archive."""
        first = package_build_bytes(_rich_inputs(), signer=_signer())
        reordered = _inputs(
            readme_bytes=b"# Acme Invoice\n",
            manifest_bytes=VALID_MANIFEST.replace(
                b"configuration:\n  schema: config.schema.json\n",
                b"configuration:\n  schema: config.schema.json\nassets:\n  - assets/logo.png\n",
            ),
            dependency_wheels={
                "other_lib-3.1.0-py3-none-any.whl": valid_dependency_wheel("other-lib", "3.1.0"),
                "helper_lib-2.0.0-py3-none-any.whl": valid_dependency_wheel("helper-lib", "2.0.0"),
            },
            assets={"assets/logo.png": b"\x89PNG\r\n\x1a\n"},
        )
        assert package_build_bytes(reordered, signer=_signer()) == first

    def test_rebuild_produces_the_same_content_digest_across_signer_seeds(self) -> None:
        """The content digest signs the *content*, so a different key must not change it.

        This is the distinction that keeps `content_digest` and `archive_digest` from being
        confused: the archive embeds the signature, so it changes with the key, while the signed
        content does not.
        """
        first = verify_package(archive_bytes=package_build_bytes(_rich_inputs(), signer=_signer()))
        second = verify_package(
            archive_bytes=package_build_bytes(
                _rich_inputs(), signer=_signer(OTHER_TEST_SIGNING_SEED)
            )
        )
        assert first.content_digest == second.content_digest
        assert first.archive_digest != second.archive_digest
        assert first.signer_fingerprint != second.signer_fingerprint

    def test_signature_is_deterministic_for_one_key(self) -> None:
        first = verify_package(archive_bytes=package_build_bytes(_rich_inputs(), signer=_signer()))
        second = verify_package(archive_bytes=package_build_bytes(_rich_inputs(), signer=_signer()))
        assert first.signer_public_key == second.signer_public_key
        assert first.signer_fingerprint == second.signer_fingerprint


class TestNonReproducibilityIsVisible:
    """Different inputs must produce different outputs, or the digests prove nothing."""

    def test_changed_manifest_changes_the_digest(self) -> None:
        base = verify_package(archive_bytes=package_build_bytes(_inputs(), signer=_signer()))
        changed_manifest = VALID_MANIFEST.replace(
            b"display_name: Acme Invoice Agent", b"display_name: Acme Invoice Agent!"
        )
        changed = verify_package(
            archive_bytes=package_build_bytes(
                _inputs(manifest_bytes=changed_manifest), signer=_signer()
            )
        )
        assert base.content_digest != changed.content_digest

    def test_changed_config_schema_changes_the_digest(self) -> None:
        base = verify_package(archive_bytes=package_build_bytes(_inputs(), signer=_signer()))
        altered_schema = VALID_CONFIG_SCHEMA.replace(b'"hello"', b'"howdy"')
        assert altered_schema != VALID_CONFIG_SCHEMA
        changed = verify_package(
            archive_bytes=package_build_bytes(
                _inputs(config_schema_bytes=altered_schema), signer=_signer()
            )
        )
        assert base.content_digest != changed.content_digest

    def test_changed_agent_wheel_changes_the_digest(self) -> None:
        base = verify_package(archive_bytes=package_build_bytes(_inputs(), signer=_signer()))
        altered = build_wheel_bytes(members={"acme_invoice_agent/extra.py": b"X = 1\n"})
        changed = verify_package(
            archive_bytes=package_build_bytes(_inputs(agent_wheel_bytes=altered), signer=_signer())
        )
        assert base.content_digest != changed.content_digest

    def test_changed_asset_changes_the_digest(self) -> None:
        manifest = VALID_MANIFEST.replace(
            b"configuration:\n  schema: config.schema.json\n",
            b"configuration:\n  schema: config.schema.json\nassets:\n  - assets/logo.png\n",
        )
        first = verify_package(
            archive_bytes=package_build_bytes(
                _inputs(manifest_bytes=manifest, assets={"assets/logo.png": b"one"}),
                signer=_signer(),
            )
        )
        second = verify_package(
            archive_bytes=package_build_bytes(
                _inputs(manifest_bytes=manifest, assets={"assets/logo.png": b"two"}),
                signer=_signer(),
            )
        )
        assert first.content_digest != second.content_digest

    def test_changed_dependency_wheel_changes_the_digest(self) -> None:
        first = verify_package(
            archive_bytes=package_build_bytes(
                _inputs(
                    dependency_wheels={
                        "helper_lib-2.0.0-py3-none-any.whl": valid_dependency_wheel(
                            "helper-lib", "2.0.0"
                        )
                    }
                ),
                signer=_signer(),
            )
        )
        second = verify_package(
            archive_bytes=package_build_bytes(
                _inputs(
                    dependency_wheels={
                        "helper_lib-2.0.0-py3-none-any.whl": build_wheel_bytes(
                            name="helper_lib",
                            version="2.0.0",
                            metadata_name="helper-lib",
                            metadata_version="2.0.0",
                            members={"helper_lib/extra.py": b"Y = 2\n"},
                        )
                    }
                ),
                signer=_signer(),
            )
        )
        assert first.content_digest != second.content_digest


@pytest.mark.parametrize("seed", [TEST_SIGNING_SEED, OTHER_TEST_SIGNING_SEED])
def test_reproducibility_holds_for_any_deterministic_signer(seed: bytes) -> None:
    first = package_build_bytes(_rich_inputs(), signer=_signer(seed))
    second = package_build_bytes(_rich_inputs(), signer=_signer(seed))
    assert first == second
