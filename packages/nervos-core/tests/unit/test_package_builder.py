"""G2 builder behaviour: assembly, output safety, and self-verification.

The builder's job is to be unable to produce something the verifier would reject. That is asserted
here rather than assumed, because a builder and verifier that disagree is precisely the failure this
milestone exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nervos_core.application.package_builder import (
    ALLOWED_TOP_LEVEL,
    PackageBuildError,
    PackageBuildInputs,
    package_build,
    package_build_bytes,
)
from nervos_core.application.package_manifest import parse_package_manifest
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_verification import verify_package
from package_fixtures import (
    TEST_SIGNING_SEED,
    VALID_CONFIG_SCHEMA,
    VALID_MANIFEST,
    build_wheel_bytes,
    valid_dependency_wheel,
    valid_wheel,
)


def _signer() -> Ed25519PackageSigner:
    return Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED)


def _inputs(**overrides: object) -> PackageBuildInputs:
    base: dict[str, object] = {
        "manifest_bytes": VALID_MANIFEST,
        "config_schema_bytes": VALID_CONFIG_SCHEMA,
        "agent_wheel_bytes": valid_wheel(),
    }
    base.update(overrides)
    return PackageBuildInputs(**base)  # type: ignore[arg-type]


_A_MANIFEST_WITH_ASSETS = VALID_MANIFEST.replace(
    b"configuration:\n  schema: config.schema.json\n",
    b"configuration:\n  schema: config.schema.json\nassets:\n  - assets/logo.png\n",
)


class TestAssembly:
    def test_minimal_package_builds_and_verifies(self) -> None:
        raw = package_build_bytes(_inputs(), signer=_signer())
        verified = verify_package(archive_bytes=raw)
        assert verified.manifest.package_id == "com.acme.invoice"
        assert verified.manifest.package_version == "1.2.3"

    def test_manifest_bytes_are_stored_unmodified(self) -> None:
        """The exact author bytes are the signed payload, so they must survive byte for byte."""
        from nervos_core.application.package_archive import (
            ArchiveValidationProfile,
            BoundedArchiveReader,
        )

        raw = package_build_bytes(_inputs(), signer=_signer())
        reader = BoundedArchiveReader(raw, profile=ArchiveValidationProfile.NERVOS_V1)
        assert reader.read("manifest.yaml") == VALID_MANIFEST

    def test_payload_manifest_excludes_integrity_members(self) -> None:
        raw = package_build_bytes(_inputs(), signer=_signer())
        verified = verify_package(archive_bytes=raw)
        paths = {entry.path for entry in verified.entries}
        assert "integrity/files.json" not in paths
        assert "integrity/signature.json" not in paths

    def test_payload_manifest_covers_every_payload(self) -> None:
        raw = package_build_bytes(_inputs(), signer=_signer())
        verified = verify_package(archive_bytes=raw)
        paths = {entry.path for entry in verified.entries}
        assert {
            "manifest.yaml",
            "agent.whl",
            "config.schema.json",
            "dependencies/lock.json",
        } <= paths

    def test_optional_readme_is_included_when_supplied(self) -> None:
        raw = package_build_bytes(_inputs(readme_bytes=b"# Acme\n"), signer=_signer())
        verified = verify_package(archive_bytes=raw)
        assert "README.md" in {entry.path for entry in verified.entries}

    def test_readme_is_omitted_when_absent(self) -> None:
        raw = package_build_bytes(_inputs(), signer=_signer())
        verified = verify_package(archive_bytes=raw)
        assert "README.md" not in {entry.path for entry in verified.entries}

    def test_declared_assets_round_trip(self) -> None:
        raw = package_build_bytes(
            PackageBuildInputs(
                manifest_bytes=_A_MANIFEST_WITH_ASSETS,
                config_schema_bytes=VALID_CONFIG_SCHEMA,
                agent_wheel_bytes=valid_wheel(),
                assets={"assets/logo.png": b"\x89PNG\r\n"},
            ),
            signer=_signer(),
        )
        verified = verify_package(archive_bytes=raw)
        assert "assets/logo.png" in {entry.path for entry in verified.entries}

    def test_dependency_wheels_round_trip(self) -> None:
        wheel = valid_dependency_wheel("helper-lib", "2.0.0")
        raw = package_build_bytes(
            _inputs(dependency_wheels={"helper_lib-2.0.0-py3-none-any.whl": wheel}),
            signer=_signer(),
        )
        verified = verify_package(archive_bytes=raw)
        assert "dependencies/wheels/helper_lib-2.0.0-py3-none-any.whl" in {
            entry.path for entry in verified.entries
        }
        assert len(verified.dependencies) == 1

    def test_archives_are_written_in_a_closed_layout(self) -> None:
        """Every emitted member sits under one of the frozen top-level entries."""
        from nervos_core.application.package_archive import (
            ArchiveValidationProfile,
            BoundedArchiveReader,
        )

        raw = package_build_bytes(
            _inputs(
                readme_bytes=b"# r\n",
                dependency_wheels={"helper_lib-2.0.0-py3-none-any.whl": valid_dependency_wheel()},
            ),
            signer=_signer(),
        )
        for path in BoundedArchiveReader(raw, profile=ArchiveValidationProfile.NERVOS_V1).paths():
            assert path.split("/", 1)[0] in ALLOWED_TOP_LEVEL


class TestAssemblyRejections:
    def test_undeclared_asset_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            package_build_bytes(_inputs(assets={"assets/logo.png": b"x"}), signer=_signer())

    def test_declared_asset_that_is_missing_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            package_build_bytes(
                _inputs(manifest_bytes=_A_MANIFEST_WITH_ASSETS, assets={}), signer=_signer()
            )

    def test_invalid_manifest_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            package_build_bytes(
                _inputs(manifest_bytes=b"manifest_version: '1'\n"), signer=_signer()
            )

    def test_invalid_config_schema_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            package_build_bytes(_inputs(config_schema_bytes=b"{not json"), signer=_signer())

    def test_native_agent_wheel_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            package_build_bytes(
                _inputs(agent_wheel_bytes=build_wheel_bytes(tag="cp312-cp312-win_amd64")),
                signer=_signer(),
            )

    def test_unsatisfied_dependency_is_rejected(self) -> None:
        """Offline closure is enforced at build time, not deferred to installation."""
        agent = build_wheel_bytes(requires_dist=("missing-lib>=1.0",))
        with pytest.raises(ValueError):
            package_build_bytes(_inputs(agent_wheel_bytes=agent), signer=_signer())

    def test_satisfied_dependency_is_accepted(self) -> None:
        agent = build_wheel_bytes(requires_dist=("helper-lib>=2.0",))
        raw = package_build_bytes(
            _inputs(
                agent_wheel_bytes=agent,
                dependency_wheels={"helper_lib-2.0.0-py3-none-any.whl": valid_dependency_wheel()},
            ),
            signer=_signer(),
        )
        assert verify_package(archive_bytes=raw).manifest.package_id == "com.acme.invoice"

    @pytest.mark.parametrize("asset_name", ("archive.zip", "disguised.bin"))
    def test_nested_zip_asset_is_rejected(self, asset_name: str) -> None:
        import io
        import zipfile

        asset_declaration = (
            f"configuration:\n  schema: config.schema.json\nassets:\n  - assets/{asset_name}\n"
        ).encode()
        manifest = VALID_MANIFEST.replace(
            b"configuration:\n  schema: config.schema.json\n", asset_declaration
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("payload.txt", b"nested")
        with pytest.raises(ValueError, match="nested archive"):
            package_build_bytes(
                _inputs(
                    manifest_bytes=manifest,
                    assets={f"assets/{asset_name}": buffer.getvalue()},
                ),
                signer=_signer(),
            )

    def test_nested_tar_asset_is_rejected(self) -> None:
        import io
        import tarfile

        manifest = VALID_MANIFEST.replace(
            b"configuration:\n  schema: config.schema.json\n",
            b"configuration:\n  schema: config.schema.json\nassets:\n  - assets/data.bin\n",
        )
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo("payload.txt")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        with pytest.raises(ValueError, match="nested archive"):
            package_build_bytes(
                _inputs(manifest_bytes=manifest, assets={"assets/data.bin": buffer.getvalue()}),
                signer=_signer(),
            )

    def test_dependency_wheel_with_a_path_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            package_build_bytes(
                _inputs(
                    dependency_wheels={
                        "nested/helper-2.0.0-py3-none-any.whl": valid_dependency_wheel()
                    }
                ),
                signer=_signer(),
            )


class TestOutputSafety:
    def test_build_writes_a_verifiable_artifact(self, tmp_path: Path) -> None:
        destination = tmp_path / "com.acme.invoice-1.2.3.nervos"
        built = package_build(_inputs(), signer=_signer(), destination=destination)
        assert destination.is_file()
        assert built.size == destination.stat().st_size
        assert built.verified.content_digest == built.content_digest

    def test_existing_destination_is_refused_by_default(self, tmp_path: Path) -> None:
        destination = tmp_path / "pkg.nervos"
        destination.write_bytes(b"existing")
        with pytest.raises(PackageBuildError):
            package_build(_inputs(), signer=_signer(), destination=destination)

    def test_overwrite_must_be_explicit(self, tmp_path: Path) -> None:
        destination = tmp_path / "pkg.nervos"
        destination.write_bytes(b"existing")
        built = package_build(_inputs(), signer=_signer(), destination=destination, overwrite=True)
        assert destination.stat().st_size == built.size

    def test_no_overwrite_race_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A destination created after preflight must survive unchanged."""
        from nervos_core.application import package_builder

        destination = tmp_path / "pkg.nervos"
        real_link = package_builder.os.link

        def _race(source: str | bytes | Path, target: str | bytes | Path) -> None:
            destination.write_bytes(b"concurrent-winner")
            real_link(source, target)

        monkeypatch.setattr(package_builder.os, "link", _race)
        with pytest.raises(PackageBuildError):
            package_build(_inputs(), signer=_signer(), destination=destination)
        assert destination.read_bytes() == b"concurrent-winner"
        assert list(tmp_path.glob("*.partial")) == []

    def test_failed_build_leaves_no_partial_artifact(self, tmp_path: Path) -> None:
        """A failure must not leave a valid-looking leftover file behind."""
        destination = tmp_path / "pkg.nervos"
        with pytest.raises(ValueError):
            package_build(
                _inputs(config_schema_bytes=b"{not json"),
                signer=_signer(),
                destination=destination,
            )
        assert not destination.exists()
        assert list(tmp_path.iterdir()) == []

    def test_failed_self_verification_removes_the_artifact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the produced artifact does not verify, nothing is written to disk at all."""
        from nervos_core.application import package_builder

        destination = tmp_path / "pkg.nervos"

        def _reject(*args: object, **kwargs: object) -> None:
            raise AssertionError("verification refused")

        monkeypatch.setattr(package_builder, "verify_package", _reject)
        with pytest.raises(AssertionError):
            package_build(_inputs(), signer=_signer(), destination=destination)
        assert not destination.exists()
        assert list(tmp_path.iterdir()) == []

    def test_builder_self_verifies_its_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The builder must call the shared verifier before reporting success."""
        from nervos_core.application import package_builder

        calls: list[Path] = []
        real_verify = package_builder.verify_package

        def _record(path: Path | None = None):
            assert path is not None, "production builder must verify the temp artifact by path"
            calls.append(path)
            return real_verify(path)

        monkeypatch.setattr(package_builder, "verify_package", _record)
        package_build(_inputs(), signer=_signer(), destination=tmp_path / "pkg.nervos")
        assert calls, "builder did not verify its own output"


class TestBuilderUsesG1Validation:
    def test_builder_parses_manifest_with_the_g1_parser(self) -> None:
        """Identity comes from G1's parser, not from a second interpretation of the YAML."""
        manifest = parse_package_manifest(VALID_MANIFEST)
        assert manifest.package_id == "com.acme.invoice"
        assert manifest.identity.as_agent_definition_id().agent_key == "com.acme.invoice"

    def test_builder_emits_the_g1_projected_identity(self) -> None:
        raw = package_build_bytes(_inputs(), signer=_signer())
        verified = verify_package(archive_bytes=raw)
        definition_id = verified.manifest.identity.as_agent_definition_id()
        assert definition_id.agent_key == "com.acme.invoice"
        assert definition_id.agent_definition_version == "1.2.3"
