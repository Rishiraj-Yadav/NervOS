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

from nervos_core.application.job_execution import JobExecutionService
from nervos_core.application.lease_reclamation import LeaseReclaimer
from nervos_core.application.model_completion import ModelCompletion
from nervos_core.application.retry_policy import PRODUCTION_RETRY_POLICY
from nervos_core.application.run_execution import RunExecutor
from nervos_core.application.trusted_chat import create_builtin_handler_registry
from nervos_core.infrastructure.database import create_session_factory, create_sqlite_engine
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobExecutionPersistence
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
from nervos_worker.registry import ReclaimLoop, WorkerRegistry
from nervos_worker.service import Worker

# Bumped only by the milestone that adds a migration. C6 ships migration 0006.
EXPECTED_SCHEMA_REVISION = "0006_stage_c6_queue_partitions"

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
    execution = JobExecutionService(
        persistence,
        RunExecutor(create_builtin_handler_registry()),
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
    )


async def close_worker(composition: WorkerComposition) -> None:
    """Close owned provider clients and dispose the engine."""
    await close_model_providers(composition.providers)
    composition.engine.dispose()


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
