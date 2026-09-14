"""Architecture regression tests for the NervOS package boundaries."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
CORE_SOURCE = ROOT / "packages" / "nervos-core" / "src" / "nervos_core"
MODELS_SOURCE = ROOT / "packages" / "nervos-models" / "src" / "nervos_models"
API_SOURCE = ROOT / "apps" / "api" / "src" / "nervos_api"
API_ROUTES = API_SOURCE / "api" / "routes"
FRONTEND_SOURCE = ROOT / "apps" / "web" / "src"
ORM_MODELS = CORE_SOURCE / "infrastructure" / "database" / "models.py"

EXPECTED_TABLES = {
    "users",
    "auth_sessions",
    "agent_instances",
    "runs",
    "jobs",
    "job_attempts",
    "run_events",
}
FORBIDDEN_SUBSYSTEMS = (
    "conversation",
    "message",
    "thread",
    "job",
    "attempt",
    "queue",
    "worker",
    "lease",
    "heartbeat",
    "stream",
    "sse",
    "websocket",
    "mcp",
    "tool",
    "schedule",
    "cron",
    "webhook",
    "marketplace",
)

# C1 legitimately introduces the durable Job and Attempt execution records, so those two module
# names are no longer forbidden in core. Every later-stage subsystem below stays absent: the
# durable foundation is not a licence for conversation, session, worker, or streaming modules.
FORBIDDEN_CORE_MODULE_NAMES = ("conversation", "message", "worker", "stream")


def imported_modules(path: Path) -> set[str]:
    """Collect statically declared imports from a Python source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def python_files(path: Path) -> list[Path]:
    """Return repository Python files in deterministic order."""
    return sorted(path.rglob("*.py"))


def test_domain_has_no_sqlalchemy_or_infrastructure_imports() -> None:
    imports = {
        module for path in python_files(CORE_SOURCE / "domain") for module in imported_modules(path)
    }

    assert not any(module.startswith("sqlalchemy") for module in imports)
    assert not any("infrastructure" in module for module in imports)


def test_application_has_no_fastapi_or_api_imports() -> None:
    imports = {
        module
        for path in python_files(CORE_SOURCE / "application")
        for module in imported_modules(path)
    }

    assert not any(module.startswith("fastapi") for module in imports)
    assert not any(module.startswith("sqlalchemy") for module in imports)
    assert not any(module.startswith("nervos_core.infrastructure") for module in imports)
    assert not any(module.startswith("nervos_api") for module in imports)


def test_core_never_imports_api_or_frontend() -> None:
    imports = {module for path in python_files(CORE_SOURCE) for module in imported_modules(path)}

    assert not any(module.startswith("nervos_api") for module in imports)
    assert not any(module.startswith(("react", "apps.web")) for module in imports)


def test_provider_sdk_exists_only_in_the_models_infrastructure_package() -> None:
    core_imports = {
        module for path in python_files(CORE_SOURCE) for module in imported_modules(path)
    }
    api_imports_by_file = {path: imported_modules(path) for path in python_files(API_SOURCE)}
    model_imports = {
        module for path in python_files(MODELS_SOURCE) for module in imported_modules(path)
    }

    # B4 adds a second concrete SDK; the boundary rule is unchanged for both.
    for sdk in ("anthropic", "openai"):
        assert not any(module.startswith(sdk) for module in core_imports), sdk
        assert any(module.startswith(sdk) for module in model_imports), sdk
    assert not any(module.startswith("nervos_models") for module in core_imports)
    assert all(
        path.name == "app.py" or not any(module.startswith("nervos_models") for module in imports)
        for path, imports in api_imports_by_file.items()
    )


def test_api_routes_never_reach_persistence_or_a_provider_adapter() -> None:
    """Route modules depend on application services, never on storage or a provider SDK."""
    for path in python_files(API_ROUTES):
        imports = imported_modules(path)
        text = path.read_text(encoding="utf-8")

        assert not any(module.startswith("sqlalchemy") for module in imports), path
        assert not any(module.startswith("nervos_core.infrastructure") for module in imports), path
        assert not any(
            module.startswith(("anthropic", "openai", "nervos_models")) for module in imports
        ), path
        for forbidden in ("AgentPersistence", "SqlAlchemyAgentPersistence", "Session", "Base"):
            assert forbidden not in text, (path, forbidden)


def test_exactly_two_production_providers_are_known() -> None:
    """Portability is two reviewed adapters, not open-ended discovery."""
    text = (MODELS_SOURCE / "composition.py").read_text(encoding="utf-8")

    assert "PROVIDER_ID as ANTHROPIC_PROVIDER_ID" in text
    assert "PROVIDER_ID as OPENAI_PROVIDER_ID" in text
    assert "known=[ANTHROPIC_PROVIDER_ID, OPENAI_PROVIDER_ID]" in text
    for dynamic in ("pkgutil", "importlib", "iter_entry_points", "setuptools"):
        assert dynamic not in text, dynamic


def test_trusted_chat_and_coordinator_stay_provider_neutral() -> None:
    """No provider-specific branch may appear in the shared execution path."""
    for name in ("trusted_chat.py", "run_coordinator.py", "model_completion.py"):
        text = (CORE_SOURCE / "application" / name).read_text(encoding="utf-8")
        for provider in ("anthropic", "openai", "Anthropic", "OpenAI"):
            assert provider not in text, (name, provider)


def test_b3_route_surface_and_migration_freeze() -> None:
    """The B3 surface is exactly the approved resources, and the schema is unchanged."""
    router_text = (API_SOURCE / "api" / "router.py").read_text(encoding="utf-8").lower()
    for subsystem in FORBIDDEN_SUBSYSTEMS:
        assert subsystem not in router_text, subsystem
    assert "agent_instances_router" in router_text
    assert "runs_router" in router_text

    migrations = sorted((ROOT / "apps" / "api" / "alembic" / "versions").glob("*.py"))
    assert [path.name for path in migrations] == [
        "0001_stage_a_schema.py",
        "0002_stage_b1_agent_instances_runs.py",
        "0003_stage_c1_durable_execution.py",
    ]


def test_only_one_run_coordinator_execution_call_exists() -> None:
    """Exactly one canonical execution path: no route may drive the coordinator itself."""
    calls = sum(
        path.read_text(encoding="utf-8").count("coordinator.execute(")
        for path in python_files(API_ROUTES)
    )

    assert calls == 1


def test_b3_creation_gate_pins_the_shared_trusted_definition() -> None:
    """The creatable definition must come from the trusted handler identity, not a new literal."""
    source = (API_ROUTES / "agent_instances.py").read_text(encoding="utf-8")

    assert "CHAT_DEFINITION_ID" in source
    assert "nervos.chat" not in source


def test_c1_schema_is_frozen_without_active_worker_runtime() -> None:
    tables = set(re.findall(r'__tablename__ = "([a-z_]+)"', ORM_MODELS.read_text(encoding="utf-8")))
    assert tables == EXPECTED_TABLES
    router_text = (API_SOURCE / "api" / "router.py").read_text(encoding="utf-8").lower()
    for forbidden in ("job", "attempt", "event", "worker", "queue"):
        assert forbidden not in router_text, forbidden
    core_module_names = " ".join(path.name for path in python_files(CORE_SOURCE))
    for forbidden in FORBIDDEN_CORE_MODULE_NAMES:
        assert forbidden not in core_module_names, forbidden
    assert "active_attempt_id" not in ORM_MODELS.read_text(encoding="utf-8")
    jobs_domain = (CORE_SOURCE / "domain" / "jobs.py").read_text(encoding="utf-8")
    assert "active_attempt_id" not in jobs_domain


def test_frontend_never_references_a_provider_credential_or_sdk() -> None:
    for path in sorted(FRONTEND_SOURCE.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for forbidden in ("ANTHROPIC_API_KEY", "api_key", "nervos_models", "@anthropic-ai"):
            assert forbidden not in text, (path, forbidden)


def test_production_composition_cannot_reach_the_test_only_provider() -> None:
    """The deterministic double lives outside shipped packages and is never referenced by them."""
    for path in python_files(API_SOURCE):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("e2e_support", "e2e_app", "DeterministicCompletion"):
            assert forbidden not in text, (path, forbidden)

    main_source = (API_SOURCE / "main.py").read_text(encoding="utf-8")
    assert "create_app" in main_source


def test_base_metadata_create_all_is_not_used() -> None:
    searched_roots = [ROOT / "apps", ROOT / "packages", ROOT / "scripts", ROOT / "tests"]
    offenders = [
        path.relative_to(ROOT)
        for root in searched_roots
        for path in python_files(root)
        if "Base.metadata." + "create_all(" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
