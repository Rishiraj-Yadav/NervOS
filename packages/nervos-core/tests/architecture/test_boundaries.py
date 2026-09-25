"""Architecture regression tests for the NervOS package boundaries."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from nervos_core.domain.jobs import RunEventType

ROOT = Path(__file__).resolve().parents[4]
CORE_SOURCE = ROOT / "packages" / "nervos-core" / "src" / "nervos_core"
CORE_APPLICATION = CORE_SOURCE / "application"
CORE_INFRASTRUCTURE = CORE_SOURCE / "infrastructure" / "database"
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
    # E1 adds the two Stage E durable tables. E2-E4 then landed the scheduler loop, the webhook
    # ingress and the event-publication seam that read and write them, and E4 added the management
    # API over the same rows; every one of those paths still reaches the tables only through their
    # own persistence module. The `schedule`/`cron`/`webhook` route-surface words stay forbidden in
    # `router.py`: the management surface is one `triggers` resource, not a router per kind.
    "trigger_definitions",
    "trigger_occurrences",
    # F1 adds the four durable conversation execution-protocol tables.
    "conversations",
    "conversation_turns",
    "conversation_messages",
    "conversation_run_links",
    # F2 adds context snapshots and compactions.
    "run_context_snapshots",
    "conversation_compactions",
    # F3 adds scoped memory items and versions.
    "memory_items",
    "memory_versions",
}
FORBIDDEN_SUBSYSTEMS = (
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
# names are no longer forbidden in core. F1 introduces the conversation execution-protocol modules,
# so `conversation` is no longer forbidden either. Every later-stage subsystem below stays absent:
# worker and streaming modules remain out of scope.
FORBIDDEN_CORE_MODULE_NAMES = ("worker", "stream")


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


def test_direct_workspace_imports_are_declared_in_package_manifests() -> None:
    """Every direct NervOS workspace import must be declared in the importing package.

    The uv root workspace installs all members, which masks a missing declaration; a built wheel
    of `nervos-api` or `nervos-worker` would fail to import `nervos_mcp` at runtime. The API and
    Worker both import `nervos_mcp` directly, so both manifests must declare it as a dependency
    and point it at the workspace through `[tool.uv.sources]`.
    """
    for manifest_path in (
        ROOT / "apps" / "api" / "pyproject.toml",
        ROOT / "apps" / "worker" / "pyproject.toml",
    ):
        manifest = manifest_path.read_text(encoding="utf-8")
        assert '"nervos-mcp"' in manifest, manifest_path
        assert "nervos-mcp = { workspace = true }" in manifest, manifest_path


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


# D5 authorizes exactly one new control-plane resource: the owner-scoped MCP connection. That is
# the *only* execution-plane word the router may now contain, and it is asserted positively below,
# so this is a narrowing of one name rather than a relaxation of the guard.
D5_AUTHORIZED_ROUTER_SUBSYSTEMS = ("mcp",)


def test_b3_route_surface_and_migration_freeze() -> None:
    """The B3 surface is exactly the approved resources, and the schema is unchanged."""
    router_text = (API_SOURCE / "api" / "router.py").read_text(encoding="utf-8").lower()
    for subsystem in FORBIDDEN_SUBSYSTEMS:
        if subsystem in D5_AUTHORIZED_ROUTER_SUBSYSTEMS:
            continue
        assert subsystem not in router_text, subsystem
    assert "agent_instances_router" in router_text
    assert "runs_router" in router_text
    assert "mcp_connections_router" in router_text
    assert "conversations_router" in router_text
    assert "memories_router" in router_text

    migrations = sorted((ROOT / "apps" / "api" / "alembic" / "versions").glob("*.py"))
    assert [path.name for path in migrations] == [
        "0001_stage_a_schema.py",
        "0002_stage_b1_agent_instances_runs.py",
        "0003_stage_c1_durable_execution.py",
        "0004_stage_c3_worker_registry.py",
        "0005_stage_c5_run_cancellation.py",
        "0006_stage_c6_queue_partitions.py",
        "0007_stage_d1_tool_capability_audit.py",
        # E1 adds the two Stage E durable tables. The scheduler, the ingress and the management
        # API do not exist yet, so this migration adds no route and no execution primitive.
        "0008_stage_e1_trigger_scheduling.py",
        # F1 adds the four durable conversation execution-protocol tables and no F2/F3 surface.
        "0009_stage_f1_conversations.py",
        # F2 adds context snapshots and compactions.
        "0010_stage_f2_context_snapshots_and_compactions.py",
        # F3 adds scoped memory items and versions.
        "0011_stage_f3_scoped_memory.py",
        # F4 adds conversation lifecycle (active/archived/deleted) state.
        "0012_stage_f4_conversation_lifecycle.py",
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
        "conversations.py",
        "health.py",
        # D5 is authorized exactly one new control-plane resource family: MCP connections.
        "mcp_connections.py",
        # F3 is authorized the memories resource family.
        "memories.py",
        "runs.py",
        "setup.py",
        # E4 is authorized exactly one new control-plane resource family: triggers.
        # F1 adds the conversation resource family.
        "triggers.py",
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


def test_the_tool_loop_reaches_audit_only_through_the_invocation_port() -> None:
    """D6 moved where the rule applies, and did not weaken it.

    D4 forbade the loop from emitting events at all, because the public surface did not exist yet.
    D6 publishes tool events, so the loop now *does* cause them -- but it still names none of the
    event vocabulary: durability is reached only through the invocation persistence port, whose
    implementations append the matching event in the same transaction as the transition. That
    keeps the loop's decisions testable without a database and keeps the event layer from becoming
    a second place where tool semantics are decided.
    """
    loop = (CORE_APPLICATION / "tool_loop.py").read_text(encoding="utf-8")
    for forbidden in ("RunEventType", "run_event", "_append_event", "RunEvent"):
        assert forbidden not in loop, forbidden
    # The one door it may use, and the only vocabulary it needs to read the result.
    assert "record_pre_dispatch_refusal" in loop
    assert "RefusalOutcomeKind" in loop


def test_the_tool_event_vocabulary_matches_the_frozen_schema() -> None:
    """The domain enum and the `run_events` CHECK cannot drift, because they are compared here.

    D6 found the two out of step: `0007` accepted six `tool.*` strings that `RunEventType` could
    not represent, so reading one back raised. A test that compares them is what keeps that from
    happening again, and it is also why `tool.cancelled` can never be added by accident -- it is
    absent from the migration, so an enum member for it would fail here.
    """
    migration = (
        ROOT / "apps" / "api" / "alembic" / "versions" / "0007_stage_d1_tool_capability_audit.py"
    )
    # The migration is the authority; the enum must be a subset of the strings it accepts.
    text = migration.read_text(encoding="utf-8")
    members = {member.value for member in RunEventType}
    for value in members:
        assert f"'{value}'" in text, value
    tool_types = {value for value in members if value.startswith("tool.")}
    assert tool_types == {
        "tool.requested",
        "tool.started",
        "tool.succeeded",
        "tool.failed",
        "tool.denied",
        "tool.ambiguous",
    }
    # `tool.cancelled` is deliberately absent from the *accepted vocabulary*. The migration's
    # prose explains why it is absent, so the assertion is made against the CHECK constant itself
    # rather than the file text, which would only be testing the docstring.
    accepted = re.search(r"TOOL_EVENT_TYPES\s*=\s*\((.*?)\)", text, re.DOTALL)
    assert accepted is not None, "the migration declares no tool event vocabulary"
    assert "tool.cancelled" not in accepted.group(1)


def test_run_events_are_never_read_to_decide_anything() -> None:
    """The timeline is an append-only projection, never an input to a decision.

    A Run Event is a *safe fact about the past*. If permission, retry, replay or availability ever
    consulted it, the projection would become a second authority beside `tool_invocations` and
    D2's live predicate -- and a fact that can be absent, late, or reordered is exactly the wrong
    thing to authorize with.

    Naming `RunEventType` is allowed: appending an event means naming its type, and the invocation
    persistence and the review set do exactly that. What no deciding module may do is *read* the
    table -- query it, count it, or hydrate a row from it.
    """
    deciding_modules = (
        CORE_APPLICATION / "tool_permissions.py",
        CORE_APPLICATION / "tool_loop.py",
        CORE_APPLICATION / "retry_policy.py",
        CORE_INFRASTRUCTURE / "tools.py",
        CORE_INFRASTRUCTURE / "tool_invocations.py",
    )
    readers = ("RunEventRecord", "list_run_events", "read_run_events", "run_event_from_record")
    for path in deciding_modules:
        text = path.read_text(encoding="utf-8")
        for forbidden in readers:
            assert forbidden not in text, (path.name, forbidden)

    # Only the invocation lifecycle may know the table exists at all, and only to append through
    # the shared primitive. Every other deciding module must not reference it in any form.
    for path in deciding_modules[:-1]:
        assert "run_events" not in path.read_text(encoding="utf-8"), path.name
    lifecycle = deciding_modules[-1].read_text(encoding="utf-8")
    assert "append_event_on_connection" in lifecycle


# --- D5: the MCP client lives in one package, and reaches only public SDK surfaces ---

NERVOS_MCP_SOURCE = ROOT / "packages" / "nervos-mcp" / "src" / "nervos_mcp"
NERVOS_MCP_TESTS = ROOT / "packages" / "nervos-mcp" / "tests"


def _private_sdk_imports(path: Path) -> list[str]:
    """Imports of an underscored module *inside* the MCP SDK, which is not a supported surface."""
    private: list[str] = []
    for module in imported_modules(path):
        parts = module.split(".")
        if parts[0] != "mcp":
            continue
        # A path such as `mcp.client._transport` crosses a private segment. A private helper is not
        # part of the SDK's contract, so depending on one would break silently on a patch upgrade.
        if any(segment.startswith("_") for segment in parts[1:]):
            private.append(module)
    return private


def test_the_tool_audit_path_stays_provider_neutral() -> None:
    """Audit, reconciliation and recovery must not learn what a model provider is.

    D6 runs inside the Stage C engine and beside the D4 loop, both of which are provider-blind.
    A provider name reaching the audit or recovery path would mean a durable timeline that reads
    differently depending on which adapter produced it.
    """
    modules = (
        CORE_INFRASTRUCTURE / "run_events.py",
        CORE_INFRASTRUCTURE / "tool_invocations.py",
        CORE_INFRASTRUCTURE / "jobs.py",
    )
    for path in modules:
        text = path.read_text(encoding="utf-8")
        for provider in ("anthropic", "openai", "Anthropic", "OpenAI"):
            assert provider not in text, (path.name, provider)


def test_no_tool_queue_continuation_or_resume_primitive_exists() -> None:
    """D6 integrated the tool layer with the existing engine; it did not add a second one.

    A tool-specific queue, worker, job or attempt -- or any checkpoint/resume machinery -- would be
    a new durable execution primitive, and ADR 0017's whole design is that one Job and one Attempt
    carry the entire Think -> Act -> Observe loop. Stage F owns conversation resume; D6 owns none
    of it.
    """
    forbidden = (
        "ToolJob",
        "ToolQueue",
        "ToolWorker",
        "ToolAttempt",
        "tool_scheduler",
        "checkpoint",
        "checkpointer",
        "resume_from",
        "resume_attempt",
    )
    for path in python_files(CORE_SOURCE):
        text = path.read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in text, (path.name, name)


def test_the_public_run_event_model_carries_only_safe_fields() -> None:
    """The published event names the invocation and nothing else about it.

    `tool_invocation_id` is deliberately opaque: an identifier names no tool, no argument, no
    result and no connection. Enriching the projection into a tool name or a server message is the
    change this guard exists to make someone argue for.
    """
    schema = (API_SOURCE / "api" / "schemas.py").read_text(encoding="utf-8")
    response = re.search(
        r"class RunEventResponse\(BaseModel\):(.*?)(?=\nclass )", schema, re.DOTALL
    )
    assert response is not None, "RunEventResponse is not declared in the API schemas"
    body = response.group(1)
    for forbidden in (
        "arguments",
        "result_digest",
        "result_bytes",
        "permission_decision",
        "provider_call_id",
        "upstream_name",
        "model_name",
        "source_kind",
        "credential",
    ):
        assert forbidden not in body, forbidden
    # The one link D6 added is present, so this is a narrowing check and not a vacuous one.
    assert "tool_invocation_id" in body


def test_the_frontend_gained_only_event_vocabulary() -> None:
    """The frontend change is compatibility repair for the new event types, not a new surface.

    The Run timeline validates `event_type` from a runtime allow-list with no fallback, so
    publishing tool events without teaching the client those six literals would break the whole
    page for any tool-using Run. Anything beyond the six literals -- a page, a capability control,
    a tool browser -- belongs to a milestone that authorizes it.
    """
    timeline = (FRONTEND_SOURCE / "components" / "RunTimeline.tsx").read_text(encoding="utf-8")
    for literal in (
        "tool.requested",
        "tool.started",
        "tool.succeeded",
        "tool.failed",
        "tool.denied",
        "tool.ambiguous",
    ):
        assert literal in timeline, literal
    # No tool-management surface was smuggled in beside the copy. These are identifiers a
    # capability UI would have to introduce, so they do not collide with ordinary prose.
    for forbidden in (
        "Capability",
        "Grant",
        "McpConnection",
        "ToolBrowser",
        "useTool",
        "useMcp",
        "mcpConnections",
    ):
        assert forbidden not in timeline, forbidden


def test_the_migration_head_is_exactly_0012_and_no_0013_exists() -> None:
    """F4 adds conversation lifecycle, and nothing beyond that head."""
    versions = ROOT / "apps" / "api" / "alembic" / "versions"
    discovered = sorted(path.name for path in versions.glob("*.py"))
    assert discovered[-1] == "0012_stage_f4_conversation_lifecycle.py"
    assert not any(name.startswith("0013") for name in discovered), discovered


def test_nervos_mcp_depends_only_on_public_sdk_surfaces() -> None:
    """A private SDK import would be an undeclared dependency on an implementation detail."""
    offenders = {
        str(path.relative_to(ROOT)): found
        for path in python_files(NERVOS_MCP_SOURCE)
        if (found := _private_sdk_imports(path))
    }
    assert not offenders


def test_nervos_mcp_never_reaches_the_model_provider_adapters() -> None:
    """The MCP package is a tool boundary, not a model boundary."""
    for path in python_files(NERVOS_MCP_SOURCE):
        imports = imported_modules(path)
        assert not any(module.startswith("nervos_models") for module in imports), path
        assert not any(
            module.startswith(sdk) for sdk in ("anthropic", "openai") for module in imports
        ), path


def test_the_loopback_permissive_egress_policy_is_not_reachable_from_production() -> None:
    """Tests need to dial a loopback fake server. Nothing shipped may be able to.

    The production policy refuses loopback, private, link-local and metadata addresses with no
    override. A permissive implementation therefore cannot live under `src/` at all: if it did, some
    future composition root could select it, and no guard here could tell that apart from a bug.

    This looks for a relaxation *definition* rather than the word itself, because the production
    module's own docstring explains that the permissive policy lives in the test support package --
    prose that would otherwise read as the thing it forbids.
    """
    for path in python_files(NERVOS_MCP_SOURCE):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                assert "permissive" not in node.name.lower(), path
            if isinstance(node, ast.Name):
                assert "allow_loopback" not in node.id, path


# The one file in the control plane allowed to name the concrete MCP implementation, for the same
# reason `apps/worker/src/nervos_worker/app.py` is the one allowed to name a provider adapter: a
# composition root is where a port is bound to an implementation. Every other module -- and every
# route -- stays free of it, which is what the guard above and below assert.
API_COMPOSITION_ROOT = API_SOURCE / "app.py"


def test_the_control_plane_routes_cannot_reach_the_mcp_sdk() -> None:
    """The API configures and selects a connection; it never speaks MCP itself."""
    for path in python_files(API_SOURCE):
        imports = imported_modules(path)
        assert not any(module == "mcp" or module.startswith("mcp.") for module in imports), path
        if path == API_COMPOSITION_ROOT:
            continue
        assert not any(module.startswith("nervos_mcp") for module in imports), path
    for path in python_files(API_ROUTES):
        imports = imported_modules(path)
        assert not any(module.startswith("nervos_mcp") for module in imports), path


def test_only_a_composition_root_may_name_the_concrete_mcp_implementation() -> None:
    """A binding of port to implementation belongs in a composition root and nowhere else."""
    namers = {
        path.relative_to(ROOT).as_posix()
        for path in (*python_files(API_SOURCE), *python_files(WORKER_SOURCE))
        if any(module.startswith("nervos_mcp") for module in imported_modules(path))
    }
    assert namers <= {
        API_COMPOSITION_ROOT.relative_to(ROOT).as_posix(),
        "apps/worker/src/nervos_worker/app.py",
        "apps/worker/src/nervos_worker/mcp.py",
    }, namers


# ------------------------------------------------------------------------------------------------
# Stage E (E1) -- the durable foundation, and the boundaries it must not cross
# ------------------------------------------------------------------------------------------------


def test_stage_e_application_protocols_carry_no_persistence_or_http_type() -> None:
    """A port that names a connection has made the persistence technology part of its interface.

    ADR 0020's whole point is that the connection-bound helper stays infrastructure-private, so the
    application layer keeps saying "materialize this command" rather than "write these rows".
    """
    for path in (
        CORE_SOURCE / "application" / "triggers.py",
        CORE_SOURCE / "domain" / "triggers.py",
    ):
        imports = imported_modules(path)
        for forbidden in ("sqlalchemy", "fastapi", "starlette", "uvicorn", "croniter", "cronsim"):
            assert not any(
                module == forbidden or module.startswith(f"{forbidden}.") for module in imports
            ), (
                path,
                forbidden,
            )
        text = path.read_text(encoding="utf-8")
        for forbidden in ("Connection", "Session", "Engine", "Request", "select(", "insert("):
            assert forbidden not in text, (path.name, forbidden)


def test_there_is_exactly_one_run_and_job_insertion_implementation() -> None:
    """E1 promoted the insertion to a shared helper; it must stay the only one.

    Manual submission and trigger materialization are two *callers* of one implementation. A second
    implementation would fork the ownership check, the admission checks and the grant cutoff, which
    is the failure ADR 0020 exists to prevent.
    """
    source = (CORE_INFRASTRUCTURE / "jobs.py").read_text(encoding="utf-8")
    assert source.count("def insert_run_and_job_on_connection(") == 1
    assert source.count("insert(RunRecord)") == 1
    assert source.count("insert(JobRecord)") == 1

    callers = {
        path.name
        for path in python_files(CORE_INFRASTRUCTURE)
        if "insert_run_and_job_on_connection(" in path.read_text(encoding="utf-8")
    }
    assert callers == {"jobs.py", "conversations.py", "triggers.py"}, callers


def test_trigger_provenance_is_never_read_to_decide_execution() -> None:
    """Reverse provenance is descriptive; a record that decides execution is not descriptive."""
    execution_modules = (
        "job_execution.py",
        "run_execution.py",
        "tool_loop.py",
        "run_cancellation.py",
        "lease_reclamation.py",
        "retry_policy.py",
    )
    for name in execution_modules:
        text = (CORE_APPLICATION / name).read_text(encoding="utf-8")
        for forbidden in ("trigger_occurrences", "TriggerOccurrence", "occurrence_for_run"):
            assert forbidden not in text, (name, forbidden)
    worker = "\n".join(path.read_text(encoding="utf-8") for path in python_files(WORKER_SOURCE))
    for forbidden in ("trigger_occurrences", "occurrence_for_run"):
        assert forbidden not in worker, forbidden


def test_stage_e_added_no_column_to_an_existing_table() -> None:
    """E1 adds two tables and touches no other, so `runs` keeps the shape every C/D guard pins."""
    models = ORM_MODELS.read_text(encoding="utf-8")
    runs = models.split('__tablename__ = "runs"', 1)[1].split("class ", 1)[0]
    for forbidden in ("trigger_occurrence_id", "origin_kind", "trigger_id"):
        assert forbidden not in runs, forbidden

    migration = (
        ROOT / "apps" / "api" / "alembic" / "versions" / "0008_stage_e1_trigger_scheduling.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("add_column", "alter_column", "rebuild_runs", "batch_alter_table"):
        assert forbidden not in migration, forbidden
    assert migration.count("op.create_table(") == 2
    assert 'down_revision = "0007_stage_d1_tool_capability_audit"' in migration


def test_the_cron_library_is_confined_to_one_infrastructure_adapter() -> None:
    """E2 evaluates schedules, so the library arrives — but in exactly one adapter and nowhere else.

    This replaces E1's `test_stage_e_adds_no_cron_library_and_no_scheduler_process`, whose stated
    premise ("E1 validates syntax only; evaluation is E2's, and no scheduler process exists yet")
    expired by design when E2 was implemented. It is replaced rather than deleted or weakened: the
    invariant it guarded — that no domain or application module depends on a cron library — is
    still enforced, and now enforced more precisely, because "not anywhere in core" is no longer
    the right statement once an adapter legitimately exists.

    Imports are read, not text, so a module that merely *mentions* the library in a docstring —
    these two do, deliberately — is not mistaken for one that depends on it.
    """
    importing = {
        path.relative_to(CORE_SOURCE).as_posix()
        for path in python_files(CORE_SOURCE)
        if any(module.split(".")[0] == "cronsim" for module in imported_modules(path))
    }
    assert importing == {"infrastructure/scheduling.py"}, importing


def test_no_scheduling_frontier_imports_a_cron_library() -> None:
    """Domain and application decide *when*; only the adapter knows how to iterate."""
    for frontier in (CORE_SOURCE / "domain", CORE_APPLICATION):
        for path in python_files(frontier):
            roots = {module.split(".")[0] for module in imported_modules(path)}
            assert not roots & _SCHEDULING_LIBRARIES, path


_SCHEDULING_LIBRARIES = frozenset({"cronsim", "croniter", "apscheduler", "celery", "kombu"})


def test_no_other_scheduler_framework_is_adopted() -> None:
    """One calculation library. No framework may own the loop, the state or the timing.

    `cronsim` itself is excluded here because it is the adopted one; where it may live is the
    subject of the confinement guard above. The Worker and the API are included, because a second
    scheduling path introduced anywhere would be a second authority for when a Run exists. Only
    `src` trees are read: test files legitimately name the forbidden frameworks in order to assert
    they are absent.
    """
    sources: list[Path] = []
    for package in ("nervos-core", "nervos-mcp", "nervos-models"):
        sources += python_files(ROOT / "packages" / package / "src")
    for app in ("api", "worker", "scheduler"):
        sources += python_files(ROOT / "apps" / app / "src")
    forbidden = _SCHEDULING_LIBRARIES - {"cronsim"}
    for path in sources:
        roots = {module.split(".")[0] for module in imported_modules(path)}
        assert not roots & forbidden, path


def test_the_scheduler_process_exists_and_owns_no_execution_surface() -> None:
    """The scheduler decides *when* a Run exists. It cannot reach anything that decides *how*."""
    scheduler = ROOT / "apps" / "scheduler" / "src" / "nervos_scheduler"
    sources = list(python_files(scheduler))
    assert sources, "the scheduler process must exist"
    for path in sources:
        for module in imported_modules(path):
            root = module.split(".")[0]
            assert root not in _EXECUTION_MODULES, (path, module)


_EXECUTION_MODULES = frozenset(
    {
        "nervos_models",
        "nervos_mcp",
        "fastapi",
        "starlette",
        "uvicorn",
        "nervos_worker",
        "nervos_api",
    }
)


def test_the_scheduler_reads_no_execution_state() -> None:
    """A schedule decision may not depend on a Job, an Attempt, a Run event or a Run status.

    Read as imports and attribute access rather than as prose: the scheduler's own docstrings say
    the words "claim" and "Job" precisely in order to record that it does none of it.
    """
    scheduler = ROOT / "apps" / "scheduler" / "src" / "nervos_scheduler"
    for path in python_files(scheduler):
        text = path.read_text(encoding="utf-8")
        for symbol in (
            "JobRecord",
            "JobAttemptRecord",
            "RunEventRecord",
            "RunRecord",
            "JobExecutionService",
            "LeaseReclaimer",
            "RunExecutor",
        ):
            assert symbol not in text, (path, symbol)
    modules_used = {
        module
        for path in python_files(ROOT / "apps" / "scheduler" / "src")
        for module in imported_modules(path)
    }
    assert not any("jobs" in module or "run_events" in module for module in modules_used)


def test_the_scheduler_declares_its_own_schema_expectation() -> None:
    """Each process states its own revision; neither imports the other's answer."""
    scheduler_app = (ROOT / "apps" / "scheduler" / "src" / "nervos_scheduler" / "app.py").read_text(
        encoding="utf-8"
    )
    assert 'EXPECTED_SCHEMA_REVISION = "0012_stage_f4_conversation_lifecycle"' in scheduler_app
    assert "nervos_worker" not in scheduler_app
    main = (ROOT / "apps" / "scheduler" / "src" / "nervos_scheduler" / "main.py").read_text(
        encoding="utf-8"
    )
    assert "require_schema_revision(" in main
    assert main.index("require_schema_revision(") < main.index("run_tick(")


def test_the_scheduler_service_owns_no_sleep_and_no_clock_read() -> None:
    """Time is a parameter all the way down, which is why no scheduler test waits for a minute."""
    service = (CORE_APPLICATION / "scheduler.py").read_text(encoding="utf-8")
    for forbidden in (
        "time.sleep",
        "asyncio.sleep",
        "datetime.now",
        "datetime.utcnow",
        "time.time",
        "import time",
    ):
        assert forbidden not in service, forbidden


def test_the_due_scan_is_bounded_ordered_and_keyset_continuable() -> None:
    """One bounded page per tick, in a total order, with no wholesale load of the due set."""
    persistence = (CORE_SOURCE / "infrastructure" / "database" / "triggers.py").read_text(
        encoding="utf-8"
    )
    scan = persistence.split("def due_schedule_candidates", 1)[1].split("def materialize_schedule")[
        0
    ]
    assert ".limit(limit)" in scan
    assert "next_fire_at.asc()" in scan
    assert "id.asc()" in scan
    # The continuation predicate is the keyset pair, not an offset.
    assert "next_fire_at > after_time" in scan
    assert "TriggerDefinitionRecord.id > after_id" in scan
    # The only read is the engine's own; no ORM session may appear on this path.
    assert "Session" not in scan


def test_the_scheduler_cursor_has_no_durable_representation() -> None:
    """Scan fairness state is process-local by design and must never become a table."""
    names = {path.name for path in python_files(ROOT / "apps" / "api" / "alembic" / "versions")}
    assert "scheduler_cursor" not in " ".join(names)
    models = (CORE_SOURCE / "infrastructure" / "database" / "models.py").read_text(encoding="utf-8")
    assert "scheduler_cursor" not in models.lower()
    assert "_ = migrations"  # keep the migration set in scope for the assertion above


def test_the_occurrence_status_vocabulary_matches_the_frozen_schema() -> None:
    """The enum, the domain branches and the migration CHECK must state the same two values."""
    migration = (
        ROOT / "apps" / "api" / "alembic" / "versions" / "0008_stage_e1_trigger_scheduling.py"
    ).read_text(encoding="utf-8")
    assert "status IN ('run_created','skipped')" in migration
    assert (
        "duplicate" not in migration.split('"trigger_occurrences"', 1)[1].split("def downgrade")[0]
    )
    models = ORM_MODELS.read_text(encoding="utf-8")
    assert "status IN ('run_created','skipped')" in models


#: The complete, reviewed E4 control-plane surface: nine routes on one owner-scoped resource.
#: Pinned as (verb, path) pairs, so neither an added route nor a changed verb can slip in unnoticed.
E4_TRIGGER_ROUTES = [
    ("delete", "/{trigger_id}"),
    ("get", ""),
    ("get", "/{trigger_id}"),
    ("get", "/{trigger_id}/occurrences"),
    ("patch", "/{trigger_id}"),
    ("post", ""),
    ("post", "/{trigger_id}/disable"),
    ("post", "/{trigger_id}/enable"),
    ("post", "/{trigger_id}/rotate-secret"),
]

#: The decorator shape every module in `api/routes` uses, tolerant of the multi-line form
#: `@router.post(\n    "",` that a route carrying a response model and a status code produces.
_ROUTE_DECORATOR = re.compile(r'@router\.(get|post|patch|put|delete)\(\s*"([^"]*)"')


def test_e4_exposes_only_the_reviewed_management_surface_and_the_delivery_ingress() -> None:
    """E4 lands the trigger *management* surface; the delivery ingress stays a sibling namespace.

    This replaces the E3-era guard that asserted the management router did not exist yet. A guard
    whose subject is "the next milestone has not landed" is retired by the milestone that lands it
    -- so, exactly as the `0009` guard was, it becomes a *narrower and stronger* assertion rather
    than being deleted: the management surface is now pinned to its nine reviewed routes, the
    delivery ingress is still pinned to one POST on one path, and the control plane is proved
    unable to reach the execution plane at all.
    """
    # -- The management surface is exactly the reviewed route set -----------------------------
    triggers_path = API_ROUTES / "triggers.py"
    assert triggers_path.exists()
    triggers_route = triggers_path.read_text(encoding="utf-8")
    assert 'APIRouter(prefix="/triggers")' in triggers_route
    assert sorted(_ROUTE_DECORATOR.findall(triggers_route)) == E4_TRIGGER_ROUTES

    # There is no HTTP ingress for an internal event, and no occurrence *detail* read: an event is
    # published through the application seam, never over a route, and history is list-only.
    for forbidden in ('"/events', '"/event/', '"/internal', "/occurrences/{"):
        assert forbidden not in triggers_route, forbidden

    # The control plane mounts one resource family, and its kinds are sub-concepts of that
    # resource rather than a router each, so those words stay out of the versioned router.
    router_text = (API_SOURCE / "api" / "router.py").read_text(encoding="utf-8").lower()
    for forbidden in ("schedule", "cron", "webhook", "automation", "hook"):
        assert forbidden not in router_text, forbidden
    assert router_text.count("include_router(triggers_router)") == 1

    # -- The delivery ingress stays a sibling namespace, unchanged ----------------------------
    assert not (API_ROUTES / "hooks.py").exists()
    hooks = API_SOURCE / "hooks"
    assert sorted(path.name for path in python_files(hooks)) == [
        "__init__.py",
        "dependencies.py",
        "errors.py",
        "router.py",
        "schemas.py",
    ]
    hooks_router = (hooks / "router.py").read_text(encoding="utf-8")
    # Exactly one route, and it is a POST on a locator path.
    assert hooks_router.count("@router.post(") == 1
    assert '"/{public_id}"' in hooks_router
    for verb in ("@router.get(", "@router.put(", "@router.patch(", "@router.delete("):
        assert verb not in hooks_router, verb
    # The ingress never re-enters the branch that protects the browser API.
    assert '"/api/v1"' not in hooks_router
    assert "include_in_schema=True" not in hooks_router

    # -- The control plane cannot reach the execution plane -----------------------------------
    # A trigger decides *what may cause* a Run; it never runs one. Creation, editing, enablement,
    # deletion, credential rotation and history are owner-scoped application-service calls, so the
    # model providers, the tool loop, the MCP gateway and the claim/lease machinery are
    # unreachable from the management route module.
    forbidden_tokens = (
        "RunExecutor",
        "RunCoordinator",
        "ModelCompletion",
        "JobExecutionService",
        "LeaseReclaimer",
        "ToolLoop",
        "claim_next",
        "start_attempt",
        "renew_lease",
        "close_legacy",
        "nervos_mcp",
        "nervos_models",
        "nervos_worker",
        "nervos_scheduler",
        "anthropic",
        "openai",
        "sqlalchemy",
    )
    for name in forbidden_tokens:
        assert name not in triggers_route, name
    modules = imported_modules(triggers_path)
    for prefix in (
        "nervos_mcp",
        "nervos_models",
        "nervos_worker",
        "nervos_scheduler",
        "sqlalchemy",
        "anthropic",
        "openai",
    ):
        assert not any(module == prefix or module.startswith(f"{prefix}.") for module in modules), (
            prefix,
            sorted(modules),
        )

    # No frontend surface, exactly as before.
    frontend = "\n".join(
        path.read_text(encoding="utf-8")
        for path in python_files(FRONTEND_SOURCE)
        if path.suffix == ".py"
    )
    assert frontend == ""


#: The exact reviewed conversation control-plane surface, as (verb, path) pairs.
# F1 shipped the base surface; F4 adds the lifecycle archive/unarchive/delete.
F1_CONVERSATION_ROUTES = [
    ("delete", "/{conversation_id}"),
    ("get", ""),
    ("get", "/{conversation_id}"),
    ("get", "/{conversation_id}/turns"),
    ("post", ""),
    ("post", "/{conversation_id}/archive"),
    ("post", "/{conversation_id}/messages"),
    ("post", "/{conversation_id}/turns/{turn_id}/retry"),
    ("post", "/{conversation_id}/unarchive"),
]


def test_f1_exposes_only_the_reviewed_conversation_surface() -> None:
    """Conversations are one owner-scoped resource family with exactly the reviewed route set.

    The lifecycle archive/unarchive/delete verbs are owned by F4. There is no `/memories` route
    here (that is the memory router), no streaming, and no direct provider or storage reach: the
    control plane accepts work and never executes it.
    """
    route_path = API_ROUTES / "conversations.py"
    assert route_path.exists()
    source = route_path.read_text(encoding="utf-8")
    assert 'APIRouter(prefix="/conversations")' in source
    assert sorted(_ROUTE_DECORATOR.findall(source)) == F1_CONVERSATION_ROUTES
    for forbidden in (
        '"memories"',
        "RunExecutor",
        "RunCoordinator",
        "ModelCompletion",
        "JobExecutionService",
        "ToolLoop",
        "claim_next",
        "start_attempt",
        "nervos_models",
        "nervos_worker",
        "nervos_scheduler",
        "anthropic",
        "openai",
        "sqlalchemy",
    ):
        assert forbidden not in source, forbidden
    modules = imported_modules(route_path)
    for prefix in (
        "nervos_models",
        "nervos_worker",
        "nervos_scheduler",
        "sqlalchemy",
        "anthropic",
        "openai",
    ):
        assert not any(module == prefix or module.startswith(f"{prefix}.") for module in modules)


# ------------------------------------------------------------------------------------------------
# Stage E (E3) -- the webhook ingress, and the boundaries it must not cross
# ------------------------------------------------------------------------------------------------

#: The one namespace a webhook may be delivered to, and the only module that may serve it.
WEBHOOK_INGRESS_MODULES = (
    "apps/api/src/nervos_api/hooks/__init__.py",
    "apps/api/src/nervos_api/hooks/dependencies.py",
    "apps/api/src/nervos_api/hooks/errors.py",
    "apps/api/src/nervos_api/hooks/router.py",
    "apps/api/src/nervos_api/hooks/schemas.py",
    "packages/nervos-core/src/nervos_core/application/webhooks.py",
    "packages/nervos-core/src/nervos_core/domain/webhooks.py",
)


def _ingress_paths() -> list[Path]:
    return [ROOT / name for name in WEBHOOK_INGRESS_MODULES]


def test_the_webhook_ingress_reaches_no_execution_or_model_plane() -> None:
    """A delivery causes a Run; it never runs one.

    The ingress authenticates a caller and materializes an occurrence. Everything that executes --
    the model, the tool loop, the MCP gateway, the claim/lease machinery -- is unreachable from it,
    so "a webhook cannot execute anything" is a structural property of the import graph rather than
    a promise about what the code chooses to call.
    """
    forbidden = (
        "RunExecutor",
        "RunCoordinator",
        "ModelCompletion",
        "JobExecutionService",
        "LeaseReclaimer",
        "ToolLoop",
        "claim_next",
        "start_attempt",
        "renew_lease",
        "close_legacy",
        "nervos_mcp",
        "nervos_models",
        "nervos_worker",
        "nervos_scheduler",
        "anthropic",
        "openai",
    )
    for path in _ingress_paths():
        source = path.read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in source, (path.name, name)
        modules = imported_modules(path)
        assert not any(module.startswith("nervos_mcp") for module in modules), path.name
        assert not any(module.startswith("nervos_models") for module in modules), path.name


def test_the_webhook_modules_carry_no_http_or_persistence_type() -> None:
    """A port that names a connection has made the persistence technology part of its interface.

    ADR 0020's whole point is that the application layer keeps saying "materialize this delivery"
    rather than "write these rows", and ADR 0019's is that the ingress is a value boundary rather
    than a transport. `Request` is a forbidden *token* here, which is why the command value is
    named `WebhookDelivery` -- a name that would otherwise have leaked the transport into the
    application layer without anyone noticing.
    """
    for name in (
        "packages/nervos-core/src/nervos_core/application/webhooks.py",
        "packages/nervos-core/src/nervos_core/domain/webhooks.py",
    ):
        path = ROOT / name
        modules = imported_modules(path)
        for forbidden in ("sqlalchemy", "fastapi", "starlette", "uvicorn"):
            assert not any(
                module == forbidden or module.startswith(f"{forbidden}.") for module in modules
            ), (path.name, forbidden)
        source = path.read_text(encoding="utf-8")
        for forbidden in ("Connection", "Session", "Engine", "Request", "select(", "insert("):
            assert forbidden not in source, (path.name, forbidden)


def test_no_webhook_queue_or_attempt_primitive_exists() -> None:
    """A delivery is consumed by the existing engine; it adds no second execution primitive.

    A webhook-specific queue, worker, attempt or retry loop would be a new durable execution
    primitive beside the one Job and one Attempt the engine already runs. Stage E says a missed
    delivery is the sender's problem, and C3/C4 own everything after the Run exists.
    """
    forbidden = (
        "WebhookJob",
        "WebhookQueue",
        "WebhookWorker",
        "WebhookAttempt",
        "webhook_queue",
        "webhook_scheduler",
        "webhook_cursor",
        "delivery_attempt",
        "replay_event",
        "reprocess_delivery",
    )
    for path in python_files(CORE_SOURCE):
        source = path.read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in source, (path.name, name)


def test_no_run_event_type_was_added_for_ingress() -> None:
    """The ingress writes no RunEvent at all: `trigger_occurrences` is the ingress history.

    ADR 0018 rejects `webhook.received` and `run.triggered` as duplicates of what the occurrence
    already records, and the event vocabulary is a frozen `CHECK` that SQLite cannot alter. So the
    enum is compared against the schema that accepts it, and the ingress modules are checked for
    any reference to the event model.
    """
    migrations = (ROOT / "apps" / "api" / "alembic" / "versions").glob("*.py")
    declared = "\n".join(path.read_text(encoding="utf-8") for path in migrations)
    for member in RunEventType:
        assert f"'{member.value}'" in declared, member.value
    assert not any(
        member.value.startswith(("webhook", "trigger", "schedule")) for member in RunEventType
    )
    for path in _ingress_paths():
        source = path.read_text(encoding="utf-8")
        for forbidden in ("RunEventType", "RunEvent", "run_events", "_append_event"):
            assert forbidden not in source, (path.name, forbidden)


def test_the_webhook_secret_is_never_stored_or_compared_in_plaintext() -> None:
    """Verification is constant-time, in one place, and the column holds a digest.

    Three things have to be true at once for a credential boundary to be worth anything: the
    plaintext is never persisted, the comparison is never `==`, and there is exactly one module
    that performs it -- so a second, weaker comparison cannot be introduced somewhere else without
    this failing.
    """
    migration = (
        ROOT / "apps" / "api" / "alembic" / "versions" / "0008_stage_e1_trigger_scheduling.py"
    ).read_text(encoding="utf-8")
    models = ORM_MODELS.read_text(encoding="utf-8")
    assert "secret_digest" in migration and "secret_digest" in models
    assert "secret_digest_shape" in migration

    # The only module that compares digests is the one that owns the primitive.
    owners = {
        path.relative_to(ROOT).as_posix()
        for path in python_files(CORE_SOURCE)
        if "compare_digest" in path.read_text(encoding="utf-8")
    }
    assert owners == {
        "packages/nervos-core/src/nervos_core/infrastructure/security/webhook_secrets.py"
    }, owners

    # And the transaction reaches the comparison through that primitive, never by equality.
    triggers = (CORE_INFRASTRUCTURE / "triggers.py").read_text(encoding="utf-8")
    assert "secret_matches_digest(" in triggers
    assert "secret_digest ==" not in triggers
    assert "== definition.secret_digest" not in triggers


def test_no_raw_webhook_payload_is_stored_separately() -> None:
    """Only a digest and a byte count reach the occurrence; the body has no durable home of its own.

    The canonical rendering does become part of the Run's immutable input, because that is the only
    way a payload can reach an agent at all. What is forbidden is a *separate* store -- a table or
    a column holding the raw body, which would be a second record of one fact.
    """
    migration = (
        ROOT / "apps" / "api" / "alembic" / "versions" / "0008_stage_e1_trigger_scheduling.py"
    ).read_text(encoding="utf-8")
    models = ORM_MODELS.read_text(encoding="utf-8")
    for forbidden in (
        "webhook_payload",
        "raw_body",
        "request_body",
        "payload_text",
        "payload_json",
    ):
        assert forbidden not in migration, forbidden
        assert forbidden not in models, forbidden
    assert "payload_digest" in models and "payload_bytes" in models


def test_both_materializers_write_occurrences_through_one_writer() -> None:
    """Two orderings, one writer. A second insertion path would be a second source of occurrence
    truth, and the schedule ordering and the webhook ordering differ precisely because their
    subjects differ -- so the writer is what they must still share."""
    triggers = (CORE_INFRASTRUCTURE / "triggers.py").read_text(encoding="utf-8")
    assert triggers.count("def _write_occurrence(") == 1
    assert triggers.count("TriggerOccurrenceRecord).values(") == 1
    assert triggers.count("def _materialize_webhook_once(") == 1
    assert triggers.count("def _materialize_once(") == 1
    # Both orderings exist, and they are different: the webhook core authenticates before it looks
    # up an identity, and the shared scheduler core does the opposite.
    webhook = triggers.split("def _materialize_webhook_once(", 1)[1].split(
        "def _refresh_existing", 1
    )[0]
    assert webhook.index("secret_matches_digest(") < webhook.index("_find_existing(")
    scheduler = triggers.split("def _materialize_once(", 1)[1].split("def _materialize_new(", 1)[0]
    assert scheduler.index("_find_existing(") < scheduler.index("not definition.enabled")


def test_the_ingress_sets_no_store_and_never_redirects() -> None:
    """The management header middleware is scoped to `/api/v1`, so the ingress carries its own."""
    errors = (API_SOURCE / "hooks" / "errors.py").read_text(encoding="utf-8")
    assert '"Cache-Control": "no-store"' in errors
    assert "Retry-After" in errors
    router = (API_SOURCE / "hooks" / "router.py").read_text(encoding="utf-8")
    for forbidden in ("RedirectResponse", "status_code=301", "status_code=302", "status_code=307"):
        assert forbidden not in router, forbidden
    assert "no-store" not in router  # the header lives in one place
