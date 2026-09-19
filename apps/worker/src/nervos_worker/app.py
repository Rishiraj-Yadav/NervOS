"""Worker composition root.

This is the **only** module in the Worker that may import `nervos_models`. The Worker never
imports the API package, never constructs a provider SDK client directly, and never runs
Alembic: it validates the schema revision and refuses to start against the wrong one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nervos_core.application.builtin_tools import (
    BUILTIN_SOURCE_REF,
    builtin_tool_specs,
    create_builtin_tool_registry,
    reconcile_builtin_definitions,
)
from nervos_core.application.job_execution import JobExecutionService
from nervos_core.application.lease_reclamation import LeaseReclaimer
from nervos_core.application.model_completion import ModelCompletion
from nervos_core.application.retry_policy import PRODUCTION_RETRY_POLICY
from nervos_core.application.run_execution import RunExecutor
from nervos_core.application.tool_loop import ToolLoop
from nervos_core.application.trusted_chat import (
    NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION,
    create_builtin_handler_registry,
)
from nervos_core.domain.tools import ToolDescriptor
from nervos_core.infrastructure.database import create_session_factory, create_sqlite_engine
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobExecutionPersistence
from nervos_core.infrastructure.database.mcp_connections import (
    SqlAlchemyMcpConnectionPersistence,
)
from nervos_core.infrastructure.database.tool_definitions import (
    SqlAlchemyToolDefinitionPersistence,
)
from nervos_core.infrastructure.database.tool_invocations import (
    SqlAlchemyToolInvocationPersistence,
)
from nervos_core.infrastructure.database.tools import SqlAlchemyToolPermissionEvaluator
from nervos_mcp.gateway import McpGateway
from nervos_mcp.operator_config import load_operator_config
from nervos_mcp.policy.egress import StrictEgressPolicy
from nervos_models import (
    ModelProviderComposition,
    close_model_providers,
    compose_model_providers,
)
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from nervos_worker.config import WorkerSettings, get_worker_settings
from nervos_worker.identity import generate_worker_id
from nervos_worker.mcp import McpRegistrySynchronizer, build_mcp_gateway
from nervos_worker.registry import ReclaimLoop, WorkerRegistry
from nervos_worker.service import Worker

# Bumped only by the milestone that adds a migration. C6 ships migration 0006.
EXPECTED_SCHEMA_REVISION = "0007_stage_d1_tool_capability_audit"

SCHEMA_MIGRATION_HINT = (
    "Run `uv run alembic -c apps/api/alembic.ini upgrade head` first. "
    "The Worker never migrates the database itself."
)


class SchemaRevisionMismatch(RuntimeError):
    """The database schema is not at the revision this Worker requires."""


def utc_now() -> datetime:
    """Return the current aware UTC instant."""
    return datetime.now(UTC)


def read_schema_revision(engine: Engine) -> str | None:
    """Read the applied Alembic revision, or None when the database is uninitialized."""
    try:
        with engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    except SQLAlchemyError:
        return None
    return None if revision is None else str(revision)


def require_schema_revision(engine: Engine, expected: str = EXPECTED_SCHEMA_REVISION) -> str:
    """Fail loudly rather than executing against an unknown schema."""
    revision = read_schema_revision(engine)
    if revision is None or revision != expected:
        raise SchemaRevisionMismatch(
            f"database schema revision is {revision!r}, expected {expected!r}. "
            f"{SCHEMA_MIGRATION_HINT}"
        )
    return revision


def resolve_completions(composition: ModelProviderComposition) -> dict[str, ModelCompletion]:
    """Resolve every configured provider exactly once, at startup.

    The returned mapping is the Worker's frozen capability set; nothing resolves a provider
    again during execution.
    """
    catalog = composition.catalog
    return {provider_id: catalog.resolve(provider_id) for provider_id in catalog.configured_ids}


@dataclass(frozen=True, slots=True)
class WorkerComposition:
    """Everything one Worker process owns, composed once and closed once."""

    settings: WorkerSettings
    engine: Engine
    session_factory: sessionmaker[Session]
    persistence: SqlAlchemyJobExecutionPersistence
    providers: ModelProviderComposition
    completions: Mapping[str, ModelCompletion]
    execution: JobExecutionService
    registry: WorkerRegistry
    reclaimer: ReclaimLoop
    worker: Worker
    mcp_gateway: McpGateway
    mcp_synchronizer: McpRegistrySynchronizer


def create_worker(settings: WorkerSettings | None = None) -> WorkerComposition:
    """Compose the Worker without contacting any provider."""
    resolved = settings or get_worker_settings()
    engine = create_sqlite_engine(resolved.database_path)
    session_factory = create_session_factory(engine)
    persistence = SqlAlchemyJobExecutionPersistence(engine)
    anthropic_secret = (
        resolved.anthropic_api_key.get_secret_value()
        if resolved.anthropic_api_key is not None
        else None
    )
    openai_secret = (
        resolved.openai_api_key.get_secret_value() if resolved.openai_api_key is not None else None
    )
    providers = compose_model_providers(anthropic_secret, openai_secret)
    completions = resolve_completions(providers)
    tool_definitions = SqlAlchemyToolDefinitionPersistence(engine)
    tool_registry = create_builtin_tool_registry(tool_definitions, clock=utc_now)
    # The Worker's MCP side: one process-local cache of live clients, and the durable connection
    # rows it may execute against. The strict policy is chosen here and nowhere else -- `build_
    # mcp_gateway` deliberately takes it as an argument rather than constructing one.
    mcp_operator = load_operator_config(
        allowed_origins=resolved.mcp_allowed_origins,
        stdio_servers_json=resolved.mcp_stdio_servers,
        credential_aliases_json=resolved.mcp_credential_aliases,
    )
    mcp_connections = SqlAlchemyMcpConnectionPersistence(engine)
    mcp_gateway = build_mcp_gateway(
        connections=mcp_connections,
        operator=mcp_operator,
        policy=StrictEgressPolicy(mcp_operator.allowed_origins),
    )
    mcp_synchronizer = McpRegistrySynchronizer(
        registry=tool_registry,
        connections=mcp_connections,
        definitions=tool_definitions,
        gateway=mcp_gateway,
    )
    tool_loop = ToolLoop(
        registry=tool_registry,
        source_ref=BUILTIN_SOURCE_REF,
        authorize=SqlAlchemyToolPermissionEvaluator(engine),
        invocations=SqlAlchemyToolInvocationPersistence(engine),
        usage=persistence,
        system_instruction=NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION,
        clock=utc_now,
        # The one non-startup seam: before each Attempt assembles its catalog, the registry is
        # re-synchronised from durable connection state. This is what lets a connection created by
        # the control plane after this process started become usable without a restart.
        synchronizer=mcp_synchronizer,
    )
    execution = JobExecutionService(
        persistence,
        RunExecutor(create_builtin_handler_registry(), tool_loop=tool_loop),
        completions,
        utc_now,
        # The retry schedule is injected rather than reached for globally, so the Worker's
        # composition root names the one policy that decides when a safe failure may be
        # replayed.
        retry_policy=PRODUCTION_RETRY_POLICY,
    )
    worker_id = generate_worker_id()
    registry = WorkerRegistry(persistence, worker_id, clock=utc_now)
    reclaimer = ReclaimLoop(LeaseReclaimer(persistence), clock=utc_now)
    worker = Worker(
        persistence,
        execution,
        completions,
        clock=utc_now,
        worker_id=worker_id,
        concurrency=resolved.worker_concurrency,
        max_active=resolved.max_active_jobs,
        registry=registry,
        reclaimer=reclaimer,
    )
    return WorkerComposition(
        settings=resolved,
        engine=engine,
        session_factory=session_factory,
        persistence=persistence,
        providers=providers,
        completions=completions,
        execution=execution,
        registry=registry,
        reclaimer=reclaimer,
        worker=worker,
        mcp_gateway=mcp_gateway,
        mcp_synchronizer=mcp_synchronizer,
    )


async def close_worker(composition: WorkerComposition) -> None:
    """Close owned provider clients, live MCP sessions, and the engine."""
    await composition.mcp_gateway.close_all()
    await close_model_providers(composition.providers)
    composition.engine.dispose()


def reconcile_tool_definitions(engine: Engine) -> tuple[ToolDescriptor, ...]:
    """Make the built-in tool definitions durable, and return their descriptors.

    This runs *after* the schema revision has been validated, never during composition: a Worker
    pointed at an unrecognized schema must be refused before it writes anything, and composing a
    Worker must stay free of database writes so it can be built to inspect configuration.

    Reconciling grants nothing. It only ensures the tools D4 can execute have durable rows; a
    definition becomes usable solely through an explicit grant, and D2's live predicate still
    decides every call.
    """
    return reconcile_builtin_definitions(
        SqlAlchemyToolDefinitionPersistence(engine),
        specs=builtin_tool_specs(clock=utc_now),
        now=utc_now(),
    )


def write_ready_marker(path: Path, *, revision: str, provider_ids: tuple[str, ...]) -> None:
    """Write the test-only readiness marker.

    Production never sets `NERVOS_WORKER_READY_FILE`, so production never writes a file. The
    marker records no credential and no claim token — only the schema revision and the
    configured provider identifiers, which are already visible in the Worker's log.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"schema_revision={revision}",
                f"providers={','.join(provider_ids)}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
