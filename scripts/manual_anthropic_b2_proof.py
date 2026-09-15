"""Operator-only live Anthropic proof of the C2 durable execution path.

This script is intentionally excluded from pytest, canonical checks, clean-check, E2E, and
CI. Running it makes one separately authorized paid provider request. It never prints the
credential, prompt, model answer, hidden reasoning, or a raw provider response/error.

C2 removed the in-process awaited runner, so the only path that can reach a real provider is
the durable one: the script submits a Run, claims it exactly as a Worker would, executes the
immutable snapshot through the same `RunExecutor` the Worker uses, and reports the terminal
Run. That makes this a live proof of the shipped execution path rather than of a test seam.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from nervos_core.application.agent_definitions import create_builtin_definition_registry
from nervos_core.application.agents import AgentService
from nervos_core.application.job_execution import LEASE_DURATION, JobExecutionService
from nervos_core.application.model_providers import ModelProviderCatalog
from nervos_core.application.run_execution import RunExecutor
from nervos_core.application.trusted_chat import CHAT_DEFINITION_ID, create_builtin_handler_registry
from nervos_core.infrastructure.database import create_session_factory, create_sqlite_engine
from nervos_core.infrastructure.database.agents import SqlAlchemyAgentPersistence
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from nervos_core.infrastructure.database.models import UserRecord
from nervos_models.anthropic import AnthropicModelCompletion, create_anthropic_client

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = (Path.home() / ".nervos" / "nervos.db").resolve(strict=False)
PROOF_PROMPT = "Reply with one short sentence confirming this bounded test completed."
PROVIDER_ID = "anthropic"
WORKER_ID = "manual-proof-worker"
TIMEOUT_SECONDS = 60.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one operator-authorized live proof")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--model", required=True)
    return parser.parse_args()


def validate_database_path(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    if resolved == DEFAULT_DATABASE:
        raise ValueError("the default NervOS database is forbidden for the manual proof")
    if resolved.exists() and (resolved.is_dir() or resolved.stat().st_size > 0):
        raise ValueError("manual proof database must be a new or empty disposable file")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def migrate(path: Path) -> None:
    environment = os.environ.copy()
    environment["NERVOS_ENVIRONMENT"] = "test"
    environment["NERVOS_DATABASE_PATH"] = str(path)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "apps/api/alembic.ini"),
            "upgrade",
            "head",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


async def run_proof(path: Path, model: str, api_key: str) -> None:
    migrate(path)
    engine = create_sqlite_engine(path)
    factory = create_session_factory(engine)
    now = datetime.now(UTC)
    try:
        with factory.begin() as session:
            user = UserRecord(
                username="b2proof",
                password_hash="manual-proof-account-not-for-login",
                role="admin",
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            session.flush()
            owner_user_id = user.id

        client = create_anthropic_client(api_key, TIMEOUT_SECONDS)
        try:
            completion = AnthropicModelCompletion(client)
            providers = ModelProviderCatalog(
                [(PROVIDER_ID, lambda: completion)], known=[PROVIDER_ID]
            )
            submissions = SqlAlchemyJobPersistence(engine, max_pending=10)
            executions = SqlAlchemyJobExecutionPersistence(engine)
            service = AgentService(
                SqlAlchemyAgentPersistence(factory),
                create_builtin_definition_registry(),
                lambda: datetime.now(UTC),
                providers,
                submissions,
            )
            instance = service.create_instance(
                owner_user_id,
                CHAT_DEFINITION_ID,
                "Manual proof",
                PROVIDER_ID,
                model,
            )
            created = service.submit_run(owner_user_id, instance.id, PROOF_PROMPT)
            claim = executions.claim_next(
                worker_id=WORKER_ID,
                provider_ids=(PROVIDER_ID,),
                max_active=1,
                now=datetime.now(UTC),
                lease_duration=LEASE_DURATION,
            )
            if claim is None or claim.run_id != created.id:
                raise RuntimeError("the durable claim did not select the submitted Run")
            orchestrator = JobExecutionService(
                executions,
                RunExecutor(create_builtin_handler_registry()),
                {PROVIDER_ID: completion},
                lambda: datetime.now(UTC),
            )
            await orchestrator.execute(claim)
        finally:
            await client.close()

        run = SqlAlchemyAgentPersistence(factory).get_run(owner_user_id, created.id)
        print(f"run_id={run.id}")
        print(f"status={run.status.value}")
        print(f"provider={run.model_provider}")
        print(f"model={run.model_name}")
        print(f"finish_reason={run.finish_reason or 'none'}")
        print(f"input_tokens={run.usage.input_tokens}")
        print(f"output_tokens={run.usage.output_tokens}")
        print(f"total_tokens={run.usage.total_tokens}")
        print(f"elapsed_ms={run.elapsed_ms}")
        print(f"error_code={run.error_code or 'none'}")
        print(f"validated_output_present={run.output_text is not None}")
    finally:
        engine.dispose()


def main() -> int:
    args = parse_args()
    try:
        database = validate_database_path(args.database)
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key is None or not api_key.strip():
            raise ValueError("ANTHROPIC_API_KEY is unavailable in the process environment")
        asyncio.run(run_proof(database, args.model, api_key))
    except (ValueError, RuntimeError, sqlite3.Error, subprocess.CalledProcessError) as error:
        print(f"manual proof unavailable: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
