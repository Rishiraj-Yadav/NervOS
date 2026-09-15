"""Architecture regression tests for the NervOS package boundaries."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
CORE_SOURCE = ROOT / "packages" / "nervos-core" / "src" / "nervos_core"
CORE_APPLICATION = CORE_SOURCE / "application"
MODELS_SOURCE = ROOT / "packages" / "nervos-models" / "src" / "nervos_models"
API_SOURCE = ROOT / "apps" / "api" / "src" / "nervos_api"
API_ROUTES = API_SOURCE / "api" / "routes"
WORKER_SOURCE = ROOT / "apps" / "worker" / "src" / "nervos_worker"
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
    "workers",
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
    worker_imports_by_file = {path: imported_modules(path) for path in python_files(WORKER_SOURCE)}
    model_imports = {
        module for path in python_files(MODELS_SOURCE) for module in imported_modules(path)
    }

    # B4 adds a second concrete SDK; the boundary rule is unchanged for both.
    for sdk in ("anthropic", "openai"):
        assert not any(module.startswith(sdk) for module in core_imports), sdk
        assert any(module.startswith(sdk) for module in model_imports), sdk
        assert not any(
            module.startswith(sdk)
            for imports in worker_imports_by_file.values()
            for module in imports
        ), sdk
    assert not any(module.startswith("nervos_models") for module in core_imports)
    # Composition roots are the only modules allowed to reach the concrete adapter package.
    for imports_by_file in (api_imports_by_file, worker_imports_by_file):
        assert all(
            path.name == "app.py"
            or not any(module.startswith("nervos_models") for module in imports)
            for path, imports in imports_by_file.items()
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


def test_trusted_chat_and_execution_stay_provider_neutral() -> None:
    """No provider-specific branch may appear in the shared execution path."""
    for name in ("trusted_chat.py", "run_execution.py", "job_execution.py", "model_completion.py"):
        text = (CORE_APPLICATION / name).read_text(encoding="utf-8")
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
        "0004_stage_c3_worker_registry.py",
    ]


def test_only_one_durable_submission_call_exists() -> None:
    """Exactly one canonical acceptance path: no route may drive execution or the coordinator."""
    calls = sum(
        path.read_text(encoding="utf-8").count("submission.submit_run(")
        for path in python_files(API_ROUTES)
    )

    assert calls == 1
    api_sources = {path: path.read_text(encoding="utf-8") for path in python_files(API_SOURCE)}
    assert not any("RunCoordinator" in text for text in api_sources.values())


def test_b3_creation_gate_pins_the_shared_trusted_definition() -> None:
    """The creatable definition must come from the trusted handler identity, not a new literal."""
    source = (API_ROUTES / "agent_instances.py").read_text(encoding="utf-8")

    assert "CHAT_DEFINITION_ID" in source
    assert "nervos.chat" not in source


def test_the_persisted_schema_is_exactly_the_reviewed_table_set() -> None:
    """C2 activated the execution plane and C3 added the registry, without a pointer column."""
    tables = set(re.findall(r'__tablename__ = "([a-z_]+)"', ORM_MODELS.read_text(encoding="utf-8")))
    assert tables == EXPECTED_TABLES
    assert "queue_partitions" not in tables
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
    for root in (API_SOURCE, WORKER_SOURCE):
        for path in python_files(root):
            text = path.read_text(encoding="utf-8")
            for forbidden in (
                "e2e_support",
                "e2e_app",
                "e2e_worker",
                "deterministic",
                "DeterministicCompletion",
            ):
                assert forbidden not in text, (path, forbidden)

    main_source = (API_SOURCE / "main.py").read_text(encoding="utf-8")
    assert "create_app" in main_source


def test_the_control_plane_cannot_execute_or_hold_a_credential() -> None:
    """Acceptance and execution are different planes, and only one of them may hold a key."""
    for path in python_files(API_SOURCE):
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "RunExecutor",
            "ModelCompletion",
            "create_builtin_handler_registry",
            "RunCoordinator",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "AsyncAnthropic",
            "AsyncOpenAI",
            "anthropic_api_key",
            "openai_api_key",
            "anthropic",
            "openai",
        ):
            assert forbidden not in text, (path, forbidden)


def test_the_control_plane_cannot_claim_start_or_terminalize() -> None:
    """The persistence split makes execution-plane writes structurally unreachable here."""
    for path in python_files(API_SOURCE):
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "claim_next",
            "start_attempt",
            "renew_lease",
            "inspect_claim",
            "close_legacy",
            "SqlAlchemyJobExecutionPersistence",
        ):
            assert forbidden not in text, (path, forbidden)


def test_the_worker_cannot_control_and_exposes_no_http_surface() -> None:
    imports = {module for path in python_files(WORKER_SOURCE) for module in imported_modules(path)}
    assert not any(module.startswith("nervos_api") for module in imports)
    assert not any(module.startswith("fastapi") for module in imports)
    assert not any(module.startswith("uvicorn") for module in imports)
    assert not any(module.startswith("alembic") for module in imports)


def test_only_the_reviewed_c4_retry_surface_exists() -> None:
    """C4 activates safe execution retry — and nothing wider than retry.

    C3 shipped reclamation and a registry; C4 legitimately introduces the one retry engine
    those earlier stages deferred. The vocabulary that is *still* absent stays absent: no
    cancellation path, no recovery/reconciler module, no `retry_wait` written anywhere other
    than the reviewed execution and persistence modules, and no new public router surface.
    """
    application_names = " ".join(path.name for path in python_files(CORE_APPLICATION))
    for forbidden in ("reconcil", "recover", "cancel"):
        assert forbidden not in application_names, forbidden
    worker_names = " ".join(path.name for path in python_files(ROOT / "apps" / "worker" / "src"))
    for forbidden in ("reconcil", "recover", "cancel", "worker_table"):
        assert forbidden not in worker_names, forbidden
    assert (CORE_APPLICATION / "lease_reclamation.py").is_file()
    assert (ROOT / "apps" / "worker" / "src" / "nervos_worker" / "registry.py").is_file()
    # C4's retry policy is the one retry-named module, and it is pure: no persistence, no
    # provider SDK, no clock.
    assert (CORE_APPLICATION / "retry_policy.py").is_file()
    policy_source = (CORE_APPLICATION / "retry_policy.py").read_text(encoding="utf-8")
    for forbidden in ("sqlalchemy", "nervos_core.infrastructure", "anthropic", "openai", "sleep("):
        assert forbidden not in policy_source, forbidden
    # The durable retry state is written by exactly the modules C4 reviewed.
    for path in python_files(CORE_APPLICATION) + python_files(WORKER_SOURCE):
        text = path.read_text(encoding="utf-8")
        if "retry_wait" not in text:
            continue
        assert path.name in {"job_execution.py", "retry_policy.py"}, path
    router_text = (API_SOURCE / "api" / "router.py").read_text(encoding="utf-8").lower()
    for forbidden in ("recovery", "events", "cancel", "health/worker", "retry"):
        assert forbidden not in router_text, forbidden


def test_the_retry_engine_cannot_schedule_an_ambiguous_or_forbidden_failure() -> None:
    """Exactly one normalized code is positively safe to replay, and it is checked twice."""
    execution = (CORE_APPLICATION / "job_execution.py").read_text(encoding="utf-8")
    persistence = (CORE_SOURCE / "infrastructure" / "database" / "jobs.py").read_text(
        encoding="utf-8"
    )
    transcription = ROOT / "apps" / "worker" / "tests" / "unit" / "test_retry_disposition_policy.py"
    assert transcription.is_file()

    safe_codes = re.findall(
        r"^\s+(\w+): RetryDisposition\.SAFE_TO_RETRY,$",
        execution,
        flags=re.MULTILINE,
    )
    assert safe_codes == ["MODEL_RATE_LIMITED"]
    taxonomy = (CORE_APPLICATION / "model_completion.py").read_text(encoding="utf-8")
    assert 'MODEL_RATE_LIMITED = "model_rate_limited"' in taxonomy
    # The write transaction re-derives the same restriction from the exact code, so widening
    # the disposition map alone can never widen the replay surface.
    assert "retry_disposition is RetryDisposition.SAFE_TO_RETRY" in persistence
    assert "error_code == MODEL_RATE_LIMITED" in persistence


def test_c3_reclamation_did_not_become_a_generic_retry_processor() -> None:
    """C3 recovers lost claims; it must never read a disposition as an instruction to replay."""
    reclamation = (CORE_SOURCE / "infrastructure" / "database" / "jobs.py").read_text(
        encoding="utf-8"
    )
    reclaim_body = reclamation.split("def reclaim_next_expired_claim", 1)[1]
    reclaim_body = reclaim_body.split("def _expire_pre_start_attempt", 1)[0]

    assert "retry_disposition" not in reclaim_body
    assert "retry.scheduled" not in reclaim_body
    assert "JobStatus.RETRY_WAIT" not in reclaim_body


def test_both_provider_adapters_keep_sdk_retries_disabled() -> None:
    for name in ("anthropic.py", "openai.py"):
        text = (MODELS_SOURCE / name).read_text(encoding="utf-8")
        assert "max_retries=0" in text, name


def test_base_metadata_create_all_is_not_used() -> None:
    searched_roots = [ROOT / "apps", ROOT / "packages", ROOT / "scripts", ROOT / "tests"]
    offenders = [
        path.relative_to(ROOT)
        for root in searched_roots
        for path in python_files(root)
        if "Base.metadata." + "create_all(" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_recovery_authority_is_the_job_lease_and_never_registry_health() -> None:
    """C3 reclaims on lease expiry plus the exact claim tuple -- never on registry appearance.

    This is the C3 boundary that matters most: a Worker registry row is observability, and letting
    it authorize reclamation would mean a merely-unobserved process could have its work replayed.
    """
    jobs_persistence = (CORE_SOURCE / "infrastructure" / "database" / "jobs.py").read_text(
        encoding="utf-8"
    )
    reclamation = jobs_persistence.split("def reclaim_next_expired_claim", 1)[1]
    reclamation = reclamation.split("def _append_recovery_events", 1)[0]

    assert "JobRecord.lease_expires_at" in reclamation
    assert "JobRecord.lease_expires_at <= now" in reclamation
    # The classification is driven only by the committed execution-start boundary.
    assert "execution_started_at" in reclamation
    # No registry read reaches a reclamation decision.
    assert "WorkerRecord" not in reclamation
    assert "list_workers" not in reclamation
    assert "classify_worker" not in reclamation


def test_the_worker_registry_holds_no_credential_and_no_pointer_column() -> None:
    """The registry row is five columns of identity and liveness, and nothing else."""
    orm = ORM_MODELS.read_text(encoding="utf-8")
    worker_record = orm.split('__tablename__ = "workers"', 1)[1].split("\nclass ", 1)[0]
    for forbidden in (
        "token",
        "secret",
        "api_key",
        "password",
        "credential",
        "provider",
        "hostname",
        "pid",
    ):
        assert forbidden not in worker_record.lower(), forbidden
    # C1/C2 deliberately omitted this pointer, and C3 does not add it.
    assert "active_attempt_id" not in orm


def test_c3_added_no_run_status_and_kept_the_frozen_vocabulary() -> None:
    """`worker_recovery_exhausted` reuses `failed`; it does not introduce a Run status."""
    runs_domain = (CORE_SOURCE / "domain" / "runs.py").read_text(encoding="utf-8")
    status_block = runs_domain.split("class RunStatus", 1)[1].split("class ", 1)[0]
    for member in ("CREATED", "RUNNING", "SUCCEEDED", "FAILED"):
        assert member in status_block, member
    for forbidden in (
        "CANCELLED",
        "CANCELED",
        "ABANDONED",
        "EXPIRED",
        "ABORTED",
        "FAILED_BEFORE_START",
    ):
        assert forbidden not in status_block, forbidden
    assert "status IN ('created','running','succeeded','failed')" in ORM_MODELS.read_text(
        encoding="utf-8"
    )


def test_the_never_started_shape_is_licensed_only_by_the_exhausted_code() -> None:
    """The relaxed Run shape is gated on its code in both directions at every layer."""
    migration = next((ROOT / "apps" / "api" / "alembic" / "versions").glob("0004_*.py")).read_text(
        encoding="utf-8"
    )
    orm = ORM_MODELS.read_text(encoding="utf-8")
    domain = (CORE_SOURCE / "domain" / "runs.py").read_text(encoding="utf-8")

    # The started-`failed` branch must exclude the code, so the relaxation cannot leak ...
    assert "error_code <> 'worker_recovery_exhausted'" in orm
    assert "error_code <> {EXHAUSTED}" in migration
    # ... and the never-started branch must require it.
    assert "error_code = 'worker_recovery_exhausted'" in orm
    assert "error_code = {EXHAUSTED}" in migration
    # The migration's single literal is the one the domain defines and the allowlist carries.
    assert "'worker_recovery_exhausted'" in migration
    assert "WORKER_RECOVERY_EXHAUSTED" in domain
    assert 'WORKER_RECOVERY_EXHAUSTED = "worker_recovery_exhausted"' in domain


def test_c4_creates_no_c5_c6_c7_surface() -> None:
    """C4 retries an execution; it adds no cancellation, fairness, partition, or public surface."""
    for path in python_files(CORE_APPLICATION) + python_files(WORKER_SOURCE):
        text = path.read_text(encoding="utf-8")
        assert "cancellation.requested" not in text, path
        assert "run.cancelled" not in text, path
    orm = ORM_MODELS.read_text(encoding="utf-8")
    for forbidden_table in ("queue_partitions", "run_events_api", "worker_health"):
        assert forbidden_table not in orm, forbidden_table
    api_sources = {path: path.read_text(encoding="utf-8") for path in python_files(API_SOURCE)}
    for name, text in api_sources.items():
        assert "lease_reclamation" not in text, name
        assert "reclaim" not in text.lower(), name
        assert "WorkerRegistry" not in text, name
