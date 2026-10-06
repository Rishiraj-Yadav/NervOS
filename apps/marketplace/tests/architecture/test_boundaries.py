import ast
from pathlib import Path

from nervos_marketplace_service.infrastructure.models import HostedBase

ROOT = Path(__file__).resolve().parents[4]
SOURCE = ROOT / "apps/marketplace/src/nervos_marketplace_service"


def test_hosted_imports() -> None:
    allowed = {"nervos_core.domain.packages", "nervos_core.domain.package_installation"}
    forbidden = (
        "nervos_api",
        "nervos_worker",
        "nervos_scheduler",
        "nervos_models",
        "nervos_mcp",
        "nervos_package_host",
    )
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for name in names:
                assert not name.startswith(forbidden), (path, name)
                if name.startswith("nervos_core"):
                    static_seams = {
                        "verifier_child.py": {
                            "nervos_core.application.package_archive",
                            "nervos_core.application.package_verification",
                        },
                        "publisher_management.py": {"nervos_core.application.package_integrity"},
                        "publication.py": {"nervos_core.application.package_integrity"},
                    }
                    assert name in allowed | static_seams.get(path.name, set()), (path, name)
                if "domain" in path.parts or "application" in path.parts:
                    assert not name.startswith(("fastapi", "sqlalchemy", "boto3", "botocore"))


def test_no_reverse_imports() -> None:
    for area in (
        "apps/worker/src",
        "apps/scheduler/src",
        "packages/nervos-core/src",
        "packages/nervos-sdk/src",
        "packages/nervos-package-host/src",
    ):
        for path in (ROOT / area).rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    assert not (node.module or "").startswith("nervos_marketplace")
                if isinstance(node, ast.Import):
                    assert all(not x.name.startswith("nervos_marketplace") for x in node.names)


def test_schema_isolation() -> None:
    from nervos_core.infrastructure.database import Base

    assert HostedBase.metadata is not Base.metadata
    assert set(HostedBase.metadata.tables) == {
        "package_projects",
        "package_listings",
        "artifacts",
        "package_releases",
    }
    assert not set(HostedBase.metadata.tables).intersection(Base.metadata.tables)
    versions = ROOT / "apps/api/alembic/versions"
    # I4's retained local ticket is separate from all hosted catalog tables.
    assert len(list(versions.glob("0014_stage_i4_marketplace_install_requests.py"))) == 1
    for area in ("worker", "scheduler"):
        source = (ROOT / f"apps/{area}/src/nervos_{area}/app.py").read_text()
        assert 'EXPECTED_SCHEMA_REVISION = "0024_durable_workflows"' in source


def test_no_serving_mutations() -> None:
    for path in SOURCE.rglob("*.py"):
        if path.name == "models.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if path.name == "s3_final_artifacts.py":
                    assert node.func.attr not in {"delete_object", "create_all"}
                elif path.name == "publication_storage.py":
                    # I2 quarantine writer owns writes/deletes; serving remains read-only.
                    assert node.func.attr != "create_all"
                else:
                    assert node.func.attr not in {"put_object", "delete_object", "create_all"}


def test_no_orm_session_mutation() -> None:
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "session"
            ):
                assert node.func.attr not in {"add", "delete", "merge", "flush", "commit"}
