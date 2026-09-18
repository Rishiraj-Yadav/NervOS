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
    "queue_partitions",
    # D1 adds the durable tool, capability and audit tables. They are *persistence only*: no
    # registry, permission evaluator, MCP client or tool loop exists to read or write them yet,
    # which is why the name-level guards below are narrowed only for the schema and not for the
    # `tool`/`mcp` route surface, which stays forbidden.
    "mcp_connections",
    "tool_definitions",
    "agent_tool_grants",
    "tool_invocations",
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
        "0005_stage_c5_run_cancellation.py",
        "0006_stage_c6_queue_partitions.py",
        "0007_stage_d1_tool_capability_audit.py",
    ]
    # The Worker refuses to run against a schema it does not expect, so the pinned revision and
    # the migration head are one fact in two places. Letting them drift bricks the supervised
    # journey with a mismatch the Worker is right to report -- so they are asserted equal here.
    worker_app = (ROOT / "apps" / "worker" / "src" / "nervos_worker" / "app.py").read_text(
        encoding="utf-8"
    )
    assert f'EXPECTED_SCHEMA_REVISION = "{migrations[-1].stem}"' in worker_app


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
    """C2 activated the execution plane, C3 added the registry, C6 added fairness metadata.

    No milestone added a pointer column to `jobs`.
    """
    tables = set(re.findall(r'__tablename__ = "([a-z_]+)"', ORM_MODELS.read_text(encoding="utf-8")))
    assert tables == EXPECTED_TABLES
    # The one table C6 adds is scheduling metadata, so it must stay incapable of becoming a
    # second queue: no cached count can drift, and it holds no execution authority.
    fairness = (
        ORM_MODELS.read_text(encoding="utf-8")
        .split('__tablename__ = "queue_partitions"', 1)[1]
        .split("class ", 1)[0]
    )
    for forbidden in (
        "active_count",
        "pending_count",
        "running_count",
        "claimed_count",
        "claim_token",
        "lease",
        "status",
    ):
        assert forbidden not in fairness, forbidden
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


def test_only_the_reviewed_retry_and_cancellation_surfaces_exist() -> None:
    """C4 added retry, C5 adds cancellation — and neither adds anything wider.

    C3 shipped reclamation and a registry; C4 introduced the one retry engine those earlier
    stages deferred; C5 introduces the one owner-cancellation module. What is *still* absent
    stays absent: no recovery/reconciler module in the application layer, no `retry_wait`
    written anywhere other than the reviewed execution and persistence modules, and no new
    public router surface. C7 later added one reviewed *route* inside the existing Runs module
    without touching `router.py`, so the assertion below is unchanged: no second router, and no
    new router surface of any kind.
    """
    application_names = " ".join(path.name for path in python_files(CORE_APPLICATION))
    for forbidden in ("reconcil", "recover"):
        assert forbidden not in application_names, forbidden
    # Exactly one cancellation-named module, and it is the reviewed application service.
    assert sorted(
        path.name for path in python_files(CORE_APPLICATION) if "cancel" in path.name
    ) == ["run_cancellation.py"]
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
    # C5's cancellation service is a coordinator: it owns no database, no provider SDK, and no
    # cancellation-event vocabulary of its own. The transition belongs to persistence.
    cancellation_source = (CORE_APPLICATION / "run_cancellation.py").read_text(encoding="utf-8")
    for forbidden in ("sqlalchemy", "nervos_core.infrastructure", "anthropic", "openai"):
        assert forbidden not in cancellation_source, forbidden
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


def test_c5_added_the_cancelled_run_status_and_nothing_wider() -> None:
    """C5 adds exactly one Run status. C3's rule that recovery introduced none still holds.

    The vocabulary is now five members, and the fifth is `cancelled` alone: the never-started
    recovery closure still reuses `failed`, so `failed_before_start` and every other invented
    terminal state remain forbidden.
    """
    runs_domain = (CORE_SOURCE / "domain" / "runs.py").read_text(encoding="utf-8")
    status_block = runs_domain.split("class RunStatus", 1)[1].split("class ", 1)[0]
    for member in ("CREATED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"):
        assert member in status_block, member
    for forbidden in (
        "CANCELED",
        "ABANDONED",
        "EXPIRED",
        "ABORTED",
        "FAILED_BEFORE_START",
        "TIMED_OUT",
        "RETRYING",
    ):
        assert forbidden not in status_block, forbidden
    assert (
        "status IN ('created','running','succeeded','failed','cancelled')"
        in ORM_MODELS.read_text(encoding="utf-8")
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


def test_c5_cancellation_stays_confined_to_its_reviewed_modules() -> None:
    """Cancellation events are legitimate only in the reviewed places that write that lifecycle."""
    allowed = {
        "run_cancellation.py",  # the application service that names the transition
        "job_execution.py",  # the readback classification and the local stop
    }
    for path in python_files(CORE_APPLICATION) + python_files(WORKER_SOURCE):
        text = path.read_text(encoding="utf-8")
        if "cancellation.requested" in text or "run.cancelled" in text:
            assert path.name in allowed, path
    orm = ORM_MODELS.read_text(encoding="utf-8")
    for forbidden_table in ("run_events_api", "worker_health"):
        assert forbidden_table not in orm, forbidden_table
    api_sources = {path: path.read_text(encoding="utf-8") for path in python_files(API_SOURCE)}
    for name, text in api_sources.items():
        # The API may revoke authority over an owned Run; it may never claim, start, heartbeat,
        # terminalize, or reclaim execution.
        assert "lease_reclamation" not in text, name
        assert "reclaim" not in text.lower(), name
        assert "WorkerRegistry" not in text, name
        assert "claim_next" not in text, name
        assert "renew_lease" not in text, name


def test_c6_adds_only_the_reviewed_fairness_surface() -> None:
    """C6 adds queue control in exactly the reviewed shape, and nothing wider.

    The reviewed surface is the durable `queue_partitions` scheduling metadata, the shared policy
    constants, and the claim/admission predicates that read them. C7's arrival changes none of
    this: the API still cannot execute, no partition/cursor/queue-position concept reaches the API
    or the browser, and the fairness table stays incapable of becoming a second queue because it
    stores no count, no status, and no authority. C7 publishes durable facts about one Run, which
    is why the scheduling topology below is asserted to be exactly as private as it was here.
    """
    orm = ORM_MODELS.read_text(encoding="utf-8")
    assert orm.count('__tablename__ = "queue_partitions"') == 1
    fairness = orm.split('__tablename__ = "queue_partitions"', 1)[1].split("class ", 1)[0]
    for required in ("agent_instance_id", "last_served_attempt_id"):
        assert required in fairness, required
    for forbidden in ("active_count", "pending_count", "running_count", "claimed_count", "status"):
        assert forbidden not in fairness, forbidden
    # Fairness metadata is persistence-internal: nothing above the persistence layer reads it, so
    # no application service, domain object, route, or Worker module can invent a second ordering
    # rule from it.
    for path in (
        python_files(CORE_APPLICATION)
        + python_files(CORE_SOURCE / "domain")
        + python_files(API_SOURCE)
        + python_files(WORKER_SOURCE)
    ):
        assert "last_served_attempt_id" not in path.read_text(encoding="utf-8"), path
    # C7 adds no router surface of its own, and C6 publishes no scheduling surface.
    for path in python_files(CORE_APPLICATION) + python_files(API_SOURCE):
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in ("queue_position", "fair_share", "leader_election", "worker_dashboard"):
            assert forbidden not in text, (path, forbidden)
    router_text = (API_SOURCE / "api" / "router.py").read_text(encoding="utf-8").lower()
    for forbidden in ("attempt", "event", "worker", "sse", "stream", "partition"):
        assert forbidden not in router_text, forbidden
    for path in sorted(FRONTEND_SOURCE.rglob("*")):
        if not path.is_file():
            continue
        frontend_text = path.read_text(encoding="utf-8").lower()
        for forbidden in ("queue_partition", "queue_position", "fairness"):
            assert forbidden not in frontend_text, (path, forbidden)


def test_c7_adds_only_the_reviewed_observability_surface() -> None:
    """C7 publishes durable execution facts for one owned Run, and nothing wider.

    The Event route is the Run's own sub-resource, which is why it lives in the already-reviewed
    Runs module: adding a separate router module would have required relaxing the guards that keep
    execution-plane vocabulary out of the control-plane router, and no observability endpoint is
    worth weakening a boundary for. What C7 adds is one GET, one allow-list projection, and two
    derived fields on the Run. Every assertion below is about what it still cannot do.
    """
    assert sorted(path.name for path in python_files(API_ROUTES) if path.name != "__init__.py") == [
        "agent_instances.py",
        "auth.py",
        "health.py",
        "runs.py",
        "setup.py",
    ]
    runs_route = (API_ROUTES / "runs.py").read_text(encoding="utf-8")
    # Exactly one new route, and it is a read.
    assert runs_route.count('@router.get("/runs/{run_id}/events"') == 1
    assert runs_route.count("/runs/{run_id}/events") == 1
    for forbidden in (
        '@router.post("/runs/{run_id}/events"',
        '@router.patch("/runs/{run_id}/events"',
        '@router.put("/runs/{run_id}/events"',
        '@router.delete("/runs/{run_id}/events"',
    ):
        assert forbidden not in runs_route, forbidden
    # The observability read reaches no write primitive at all.
    for forbidden in ("insert(", "update(", "delete(", "commit(", "flush(", "add("):
        assert forbidden not in runs_route, forbidden
    # The public projection is an explicit allow-list, not a serialization of the stored row.
    schema_source = (API_SOURCE / "api" / "schemas.py").read_text(encoding="utf-8")
    projection = schema_source.split("class RunEventResponse", 1)[1].split(
        "class RunEventPageResponse", 1
    )[0]
    assert set(re.findall(r"^    (\w+): [\w\[\] |]+$", projection, flags=re.MULTILINE)) == {
        "sequence",
        "event_type",
        "created_at",
        "attempt_number",
        "code",
        "message",
        "available_at",
    }
    # The docstring deliberately *names* what the projection withholds, so only the code is
    # scanned: a field, an annotation, or a mapping entry is what could actually leak.
    code_only = re.sub(r'""".*?"""', "", projection, flags=re.DOTALL)
    for forbidden in (
        "claim_token",
        "worker_id",
        "lease",
        "heartbeat",
        "job_id",
        "attempt_id",
        "run_id",
        "partition",
        "queue_position",
        "fair_share",
    ):
        assert forbidden not in code_only, forbidden
    # C6's scheduling topology stays persistence-internal: nothing above it may read the table.
    for path in python_files(CORE_APPLICATION) + python_files(API_SOURCE):
        assert "queue_partitions" not in path.read_text(encoding="utf-8"), path
    # The derived phase is convenience, never authority: no module that mutates execution may
    # read it, so no write can branch on a projection that is stale the moment it is serialised.
    for name in (
        "job_execution.py",
        "run_cancellation.py",
        "lease_reclamation.py",
        "retry_policy.py",
        "run_execution.py",
    ):
        text = (CORE_APPLICATION / name).read_text(encoding="utf-8")
        assert "execution_phase" not in text, name
        assert "retry_available_at" not in text, name
    assert "execution_phase" not in runs_route
    # Still no Attempt, Worker, or streaming surface anywhere in the control plane.
    for path in python_files(API_SOURCE):
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in ("sse", "websocket", "text/event-stream", "streaming"):
            assert forbidden not in text, (path, forbidden)
    for path in python_files(API_ROUTES):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("/attempts", "worker_health", "/workers"):
            assert forbidden not in text, (path, forbidden)


# --------------------------------------------------------------------------------------------------
# D3 -- the tool layer. These guard the shape of the milestone, not its behaviour: the canonical
# validator is NervOS-owned, a built-in reaches no privileged resource, the registry does no
# execution work, and none of D4/D5 has leaked in.
# --------------------------------------------------------------------------------------------------

D3_DOMAIN_TOOLS = CORE_SOURCE / "domain" / "tools.py"
D3_TOOL_SCHEMA = CORE_APPLICATION / "tool_schema.py"
D3_TOOL_REGISTRY = CORE_APPLICATION / "tool_registry.py"
D3_BUILTIN_TOOLS = CORE_APPLICATION / "builtin_tools.py"


def test_the_canonical_validator_is_nervos_owned_not_a_general_purpose_library() -> None:
    """ADR 0015: the subset definition and its enforcement must not drift apart.

    A general-purpose validator would let the subset be defined in one place and enforced in
    another, and could be induced to resolve a reference. The prohibition is on the library, so it
    is asserted by name rather than by behaviour.
    """
    forbidden = ("jsonschema", "fastjsonschema", "referencing", "json_schema", "pydantic")
    for path in (D3_DOMAIN_TOOLS, D3_TOOL_SCHEMA):
        imports = imported_modules(path)
        for library in forbidden:
            assert not any(module.startswith(library) for module in imports), (path, library)


def test_no_mcp_sdk_import_reaches_core() -> None:
    """ADR 0016: the SDK lives in `packages/nervos-mcp`; only a composition root may reach it."""
    imports = {module for path in python_files(CORE_SOURCE) for module in imported_modules(path)}
    assert not any(module == "mcp" or module.startswith("mcp.") for module in imports)


def test_a_built_in_tool_reaches_no_privileged_resource() -> None:
    """No filesystem, subprocess, shell, network, credential store or database.

    Every one of these would either need Stage H's isolation to be safe, or would smuggle Stage H
    work into the first demo. `os` and `pathlib` are on the list because either would be enough to
    read the environment or the disk.
    """
    privileged = (
        "subprocess",
        "socket",
        "shutil",
        "http",
        "urllib",
        "requests",
        "httpx",
        "ctypes",
        "multiprocessing",
        "os",
        "pathlib",
        "sqlite3",
        "tempfile",
    )
    imports = imported_modules(D3_BUILTIN_TOOLS)
    for module in privileged:
        assert not any(
            candidate == module or candidate.startswith(f"{module}.") for candidate in imports
        ), module


def test_the_tool_registry_performs_no_execution_or_permission_work() -> None:
    """The registry registers and resolves. Catalog assembly, permission and the loop are not
    here."""
    text = D3_TOOL_REGISTRY.read_text(encoding="utf-8")
    for forbidden in ("check_permission", "ModelCompletion", "ToolInvocation", "tool_invocations"):
        assert forbidden not in text, forbidden


def test_the_tool_layer_is_not_executed_from_the_model_port() -> None:
    """The model port carries tool calls; it never acts on them.

    D3 asserted that no tool vocabulary appeared here at all. D4 legitimately introduces the
    provider-neutral carriers, so the guard moves to what must stay true afterwards: the port
    describes what a model asked for and knows nothing about descriptors, grants, executors,
    registries, or durable invocation state. Deciding and doing belong to the loop.
    """
    text = (CORE_APPLICATION / "model_completion.py").read_text(encoding="utf-8")
    for forbidden in (
        "ToolDescriptor",
        "ToolExecutor",
        "ToolRegistry",
        "ToolSource",
        "check_permission",
        "tool_invocations",
        "ToolDefinition",
    ):
        assert forbidden not in text, forbidden


def test_the_built_in_reconciliation_is_wired_only_into_the_worker() -> None:
    """D4 wires the finished D3 registration service, and only where execution lives.

    The control plane still cannot reconcile definitions: doing so would add a database write to
    API startup for a capability the API does not execute, and would let a request path reach into
    the execution plane's configuration.
    """
    for path in python_files(API_SOURCE):
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "reconcile_builtin_definitions",
            "create_builtin_tool_registry",
            "BuiltinToolExecutor",
            "ToolLoop",
            "tool_invocations",
        ):
            assert forbidden not in text, (path, forbidden)


def test_the_tool_loop_never_opens_a_transaction_across_an_await() -> None:
    """A tool call, a model call, and a retry wait are all outside any database transaction.

    Holding a connection across a provider await would pin the single SQLite writer for the whole
    duration of a network call, and would make a crash mid-await indistinguishable from a crash
    mid-write. D1's durable record of intent exists precisely so no transaction has to span it.
    """
    text = (CORE_APPLICATION / "tool_loop.py").read_text(encoding="utf-8")
    for forbidden in ("BEGIN IMMEDIATE", "begin_immediate", "session", "Session", "Engine"):
        assert forbidden not in text, forbidden
    # The loop's only durable writes go through injected ports, never through SQLAlchemy directly.
    assert "sqlalchemy" not in text


def test_the_tool_loop_module_imports_no_dangerous_or_provider_specific_module() -> None:
    """The loop orchestrates; it does not reach for the operating system or a provider SDK."""
    forbidden = (
        "subprocess",
        "socket",
        "shutil",
        "urllib",
        "httpx",
        "requests",
        "ctypes",
        "multiprocessing",
        "pathlib",
        "openai",
        "anthropic",
        "mcp",
    )
    for name in ("tool_loop.py", "tool_catalog.py", "tool_invocations.py"):
        imported = imported_modules(CORE_APPLICATION / name)
        for module in forbidden:
            assert module not in imported, (name, module)


def test_the_tool_catalog_is_grant_filtered_and_never_a_permission_shortcut() -> None:
    """Catalog membership is presentation, never authority.

    The catalog must consult the D2 evaluator for every candidate, and the loop must still
    re-authorize each call before dispatch. A catalog that trusted itself, or a loop that trusted
    the catalog, would be a second authority for the same decision.
    """
    catalog = (CORE_APPLICATION / "tool_catalog.py").read_text(encoding="utf-8")
    loop = (CORE_APPLICATION / "tool_loop.py").read_text(encoding="utf-8")
    assert "check_permission" in catalog
    assert "check_permission" in loop
    for forbidden in ("grant_tool", "revoke_tool", "INSERT", "insert("):
        assert forbidden not in catalog, forbidden


def test_the_tool_loop_emits_no_run_event() -> None:
    """D4 keeps the public Run timeline free of tool facts.

    `RunEventResponse` projects `event_type` and `message` with no type filter, so emitting a tool
    event here would publish it through the already accepted C7 endpoint before D6 designs that
    surface. `tool_invocations` is D4's durable tool record instead.
    """
    loop = (CORE_APPLICATION / "tool_loop.py").read_text(encoding="utf-8")
    for forbidden in ("RunEventType", "run_event", "_append_event", "RunEvent"):
        assert forbidden not in loop, forbidden
