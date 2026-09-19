"""Shared scaffolding for the D5 MCP acceptance journeys.

Every helper here composes the **shipped** objects: a real migrated schema, the real connection
persistence and its atomic reconciliation, the real discovery adapter over a real loopback MCP
server, the real gateway, source, executor, registry synchronizer, permission evaluator, and the
real D4 loop. The only test substitutions are the ones the production modules document as seams --
the loopback egress policy (production refuses plain ``http://`` outright) and a scripted model
completion (no provider may be reached from a test).

The database is always a disposable file under ``tmp_path``; ``NERVOS_DATABASE_PATH`` is pointed at
it for the duration of the migration so the developer's own ``~/.nervos/nervos.db`` is never opened.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from nervos_core.application.builtin_tools import (
    BUILTIN_SOURCE_REF,
    builtin_tool_specs,
    create_builtin_tool_registry,
    reconcile_builtin_definitions,
)
from nervos_core.application.job_execution import ClaimedAttempt
from nervos_core.application.mcp_connection_service import McpDiscovery
from nervos_core.application.mcp_connections import (
    ConnectionStatus,
    ConnectionTransport,
    McpConnectionRow,
)
from nervos_core.application.model_completion import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
    StopOutcome,
    ToolCall,
)
from nervos_core.application.tool_loop import ToolLoop
from nervos_core.application.tool_registry import ToolRegistry
from nervos_core.application.trusted_chat import (
    NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION,
    ChatOutcome,
)
from nervos_core.domain.runs import TOOL_ENABLED_LIMITS, Run, RunLimits
from nervos_core.domain.tools import ToolDescriptor, ToolSourceKind, ToolSourceRef
from nervos_core.infrastructure.database import create_sqlite_engine
from nervos_core.infrastructure.database.jobs import (
    SqlAlchemyJobExecutionPersistence,
    SqlAlchemyJobPersistence,
)
from nervos_core.infrastructure.database.mcp_connections import SqlAlchemyMcpConnectionPersistence
from nervos_core.infrastructure.database.models import (
    AgentInstanceRecord,
    AgentToolGrantRecord,
    JobRecord,
    RunRecord,
    ToolDefinitionRecord,
    ToolInvocationRecord,
)
from nervos_core.infrastructure.database.tool_definitions import (
    SqlAlchemyToolDefinitionPersistence,
)
from nervos_core.infrastructure.database.tool_invocations import (
    SqlAlchemyToolInvocationPersistence,
)
from nervos_core.infrastructure.database.tools import SqlAlchemyToolPermissionEvaluator
from nervos_mcp.adapters import build_discovery
from nervos_mcp.gateway import McpGateway
from nervos_mcp.operator_config import McpOperatorConfig
from nervos_mcp.policy.egress import EgressPolicy, EgressTarget, normalize_origin, parse_endpoint
from nervos_worker.mcp import McpRegistrySynchronizer, build_mcp_gateway
from sqlalchemy import Engine, func, insert, select, text

from .egress import LoopbackEgressPolicy

ROOT = Path(__file__).resolve().parents[4]
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
LEASE = timedelta(minutes=2)
OWNER_USER_ID = 1
ANTHROPIC = "anthropic"
MODEL_NAME = "opaque/model"


# --- database ----------------------------------------------------------------------------------


def migrate_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """Migrate a disposable database and seed one owner at the tool-enabled Agent definition.

    The Instance is ``nervos.chat`` version ``2`` -- the exact pair whose handler runs the tool
    loop. Version ``1`` is deliberately not used: that definition is the Stage B/C Chat path and
    never assembles a catalog.
    """
    database = tmp_path / "d5_acceptance.db"
    monkeypatch.setenv("NERVOS_DATABASE_PATH", str(database))
    command.upgrade(Config(str(ROOT / "apps" / "api" / "alembic.ini")), "head")
    engine = create_sqlite_engine(database)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users"
                "(username,password_hash,role,is_active,created_at,updated_at) "
                "VALUES('owner',X'00','admin',1,:n,:n)"
            ),
            {"n": NOW},
        )
        connection.execute(
            text(
                "INSERT INTO agent_instances"
                "(owner_user_id,agent_key,agent_definition_version,display_name,enabled,"
                "model_provider,model_name,created_at,updated_at) "
                "VALUES(1,'nervos.chat','2','Agent 1',1,:p,:m,:n,:n)"
            ),
            {"p": ANTHROPIC, "m": MODEL_NAME, "n": NOW},
        )
    return engine


def agent_instance_id(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(select(AgentInstanceRecord.id)).scalar_one())


# --- operator configuration and the loopback policy --------------------------------------------


def loopback_origin(endpoint: str) -> str:
    scheme, host, port = parse_endpoint(endpoint)
    return normalize_origin(scheme, host, port)


def operator_config(endpoint: str) -> McpOperatorConfig:
    """The operator config that authorizes exactly this loopback origin and nothing else."""
    return McpOperatorConfig(allowed_origins=frozenset({loopback_origin(endpoint)}))


def loopback_policy(endpoint: str) -> LoopbackEgressPolicy:
    _scheme, _host, port = parse_endpoint(endpoint)
    return LoopbackEgressPolicy({port})


class TripwireEgressPolicy:
    """An :class:`EgressPolicy` that fails loudly if anything ever asks it to approve a dial.

    Used to prove a code path performed no network I/O: the only route to opening a transport runs
    through :meth:`validate`, so a call that returns without tripping this cannot have dialled.
    """

    def __init__(self) -> None:
        self.asked = False

    def validate(self, endpoint: str) -> EgressTarget:
        self.asked = True
        raise AssertionError(f"an unexpected network dial to {endpoint}")


# --- durable MCP connections -------------------------------------------------------------------


async def discover_connection(
    engine: Engine,
    *,
    connection_id: int,
    config: McpOperatorConfig,
    policy: EgressPolicy,
    owner_user_id: int = OWNER_USER_ID,
    now: datetime = NOW,
) -> tuple[ToolDescriptor, ...]:
    """Run the real discovery against a durable connection and commit its catalog atomically."""
    connections = SqlAlchemyMcpConnectionPersistence(engine)
    row = connections.get_by_id(connection_id)
    assert row is not None
    discovery: McpDiscovery = build_discovery(config, policy)
    materials = await discovery(row)
    committed = connections.record_discovery_success(
        connection_id=connection_id,
        owner_user_id=owner_user_id,
        status=ConnectionStatus.CONNECTED,
        materials=materials,
        now=now,
    )
    assert committed is True
    return definitions_for(engine, ToolSourceRef(ToolSourceKind.MCP, connection_id))


async def add_connection(
    engine: Engine,
    *,
    endpoint: str,
    config: McpOperatorConfig,
    policy: EgressPolicy,
    display_name: str = "Fake Docs",
    owner_user_id: int = OWNER_USER_ID,
    now: datetime = NOW,
) -> tuple[int, tuple[ToolDescriptor, ...]]:
    """Create one durable HTTP connection and immediately discover its catalog for real."""
    connections = SqlAlchemyMcpConnectionPersistence(engine)
    connection_id = connections.create(
        owner_user_id=owner_user_id,
        display_name=display_name,
        transport=ConnectionTransport.HTTP,
        endpoint=endpoint,
        server_key=None,
        credential_ref=None,
        now=now,
    )
    descriptors = await discover_connection(
        engine,
        connection_id=connection_id,
        config=config,
        policy=policy,
        owner_user_id=owner_user_id,
        now=now,
    )
    return connection_id, descriptors


def connection_row(engine: Engine, connection_id: int) -> McpConnectionRow:
    row = SqlAlchemyMcpConnectionPersistence(engine).get_by_id(connection_id)
    assert row is not None
    return row


def definitions_for(engine: Engine, source_ref: ToolSourceRef) -> tuple[ToolDescriptor, ...]:
    persistence = SqlAlchemyToolDefinitionPersistence(engine)
    return tuple(
        persisted.to_descriptor()
        for persisted in persistence.list_for_source(source_ref=source_ref)
    )


def descriptor_named(descriptors: tuple[ToolDescriptor, ...], upstream_name: str) -> ToolDescriptor:
    for descriptor in descriptors:
        if descriptor.upstream_name == upstream_name:
            return descriptor
    raise AssertionError(f"no discovered definition named {upstream_name!r}")


# --- grants and the permission layer -----------------------------------------------------------


def definition_fingerprint(engine: Engine, tool_definition_id: int) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                select(ToolDefinitionRecord.fingerprint).where(
                    ToolDefinitionRecord.id == tool_definition_id
                )
            ).scalar_one()
        )


def grant(
    engine: Engine,
    *,
    tool_definition_id: int,
    instance_id: int,
    now: datetime = NOW,
) -> int:
    """Grant one definition, reviewing the fingerprint it durably has **right now**."""
    with engine.begin() as connection:
        connection.execute(
            insert(AgentToolGrantRecord).values(
                agent_instance_id=instance_id,
                tool_definition_id=tool_definition_id,
                reviewed_fingerprint=definition_fingerprint(engine, tool_definition_id),
                created_at=now,
            )
        )
    with engine.connect() as connection:
        return int(connection.execute(select(func.max(AgentToolGrantRecord.id))).scalar_one())


def permission(engine: Engine, *, run_id: int, tool_definition_id: int) -> tuple[bool, str]:
    decision = SqlAlchemyToolPermissionEvaluator(engine).check_permission(
        run_id=run_id, tool_definition_id=tool_definition_id
    )
    reason = decision.reason.name if decision.reason is not None else "allowed"
    return decision.allowed, reason


# --- Runs --------------------------------------------------------------------------------------


def submit_run(
    engine: Engine,
    *,
    instance_id: int,
    limits: RunLimits = TOOL_ENABLED_LIMITS,
    input_text: str = "please use a tool",
    now: datetime = NOW,
) -> int:
    run = SqlAlchemyJobPersistence(engine).submit(
        owner_user_id=OWNER_USER_ID,
        agent_instance_id=instance_id,
        input_text=input_text,
        limits=limits,
        now=now,
    )
    return run.id


def claim_run(engine: Engine, run_id: int, *, now: datetime = NOW) -> tuple[int, ClaimedAttempt]:
    persistence = SqlAlchemyJobExecutionPersistence(engine)
    claim = persistence.claim_next(
        worker_id="worker-1",
        provider_ids=(ANTHROPIC,),
        max_active=4,
        now=now,
        lease_duration=LEASE,
    )
    assert claim is not None
    assert claim.run_id == run_id
    assert persistence.start_attempt(claim, now=now) is True
    # `start_attempt` sets `execution_started_at`; the claim handle keeps the live lease values.
    assert persistence.inspect_claim(claim, now=now) is not None
    return claim.attempt_id, claim


def load_run(engine: Engine, run_id: int) -> Run:
    return SqlAlchemyJobExecutionPersistence(engine).load_run(run_id)


def succeed_run(
    engine: Engine,
    claim: ClaimedAttempt,
    outcome: ChatOutcome,
    *,
    elapsed_ms: int = 0,
    now: datetime = NOW,
) -> bool:
    """Terminalize the Attempt/Job/Run as succeeded through the shipped C3 write."""
    return SqlAlchemyJobExecutionPersistence(engine).succeed(
        claim,
        output_text=outcome.output_text,
        finish_reason=outcome.finish_reason,
        usage=outcome.usage,
        elapsed_ms=elapsed_ms,
        now=now,
    )


def run_row(engine: Engine, run_id: int) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            select(
                RunRecord.status,
                RunRecord.error_code,
                RunRecord.output_text,
            ).where(RunRecord.id == run_id)
        ).one()
    return {"status": row[0], "error_code": row[1], "output_text": row[2]}


def job_row(engine: Engine, run_id: int) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            select(JobRecord.status, JobRecord.attempt_count).where(JobRecord.run_id == run_id)
        ).one()
    return {"status": row[0], "attempt_count": row[1]}


def invocation_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                select(ToolInvocationRecord).order_by(ToolInvocationRecord.tool_sequence)
            )
            .mappings()
            .all()
        ]


def event_types(engine: Engine, run_id: int) -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text("SELECT event_type FROM run_events WHERE run_id=:r ORDER BY sequence"),
                {"r": run_id},
            ).scalars()
        )


# --- the loop rig ------------------------------------------------------------------------------


@dataclass(slots=True)
class LoopRig:
    """The shipped D4/D5 composition over one disposable database."""

    engine: Engine
    registry: ToolRegistry
    gateway: McpGateway
    synchronizer: McpRegistrySynchronizer
    loop: ToolLoop


async def no_wait(_seconds: float) -> None:
    """A sleeper that returns immediately, so an in-Attempt ladder costs no wall-clock time."""
    return None


class RecordingSleeper:
    """Records the delays the loop asked for without waiting them out."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def build_rig(
    engine: Engine,
    *,
    config: McpOperatorConfig,
    policy: EgressPolicy,
    builtins: bool = False,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> LoopRig:
    """Build the real registry, gateway, synchronizer, and loop over ``engine``.

    When ``builtins`` is set the durable built-in definitions are reconciled first -- exactly as the
    Worker does at startup -- and the built-in source is registered. The MCP source(s) are never
    registered here: they arrive through :meth:`McpRegistrySynchronizer.synchronize_for_run`, which
    is the seam this milestone's acceptance proves usable without a restart.
    """
    definitions = SqlAlchemyToolDefinitionPersistence(engine)
    if builtins:
        reconcile_builtin_definitions(
            definitions, specs=builtin_tool_specs(clock=lambda: NOW), now=NOW
        )
        registry = create_builtin_tool_registry(definitions, clock=lambda: NOW)
    else:
        registry = ToolRegistry()
    connections = SqlAlchemyMcpConnectionPersistence(engine)
    gateway = build_mcp_gateway(connections=connections, operator=config, policy=policy)
    synchronizer = McpRegistrySynchronizer(
        registry=registry,
        connections=connections,
        definitions=definitions,
        gateway=gateway,
    )
    loop = ToolLoop(
        registry=registry,
        source_ref=BUILTIN_SOURCE_REF,
        authorize=SqlAlchemyToolPermissionEvaluator(engine),
        invocations=SqlAlchemyToolInvocationPersistence(engine),
        usage=SqlAlchemyJobExecutionPersistence(engine, sleep=lambda _seconds: None),
        system_instruction=NERVOS_TOOL_CHAT_SYSTEM_INSTRUCTION,
        clock=lambda: NOW,
        sleep=no_wait if sleep is None else sleep,
        synchronizer=synchronizer,
    )
    return LoopRig(
        engine=engine,
        registry=registry,
        gateway=gateway,
        synchronizer=synchronizer,
        loop=loop,
    )


# --- scripted provider ---------------------------------------------------------------------------


def tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments_json=json.dumps(arguments))


def tool_turn(*calls: ToolCall) -> ModelResponse:
    return ModelResponse(
        "",
        ANTHROPIC,
        MODEL_NAME,
        StopOutcome.TOOL_USE,
        ModelUsage(1, 1, 2),
        calls,
    )


def final_turn(text: str) -> ModelResponse:
    return ModelResponse(text, ANTHROPIC, MODEL_NAME, StopOutcome.STOP, ModelUsage(1, 1, 2))


class ScriptedCompletion:
    """Returns scripted responses in order, then raises the scripted failure for every later call.

    ``beyond`` models a provider that keeps rate-limiting: the loop's in-Attempt ladder must exhaust
    without the completion double itself standing in for anything NervOS owns.
    """

    def __init__(self, *script: ModelResponse, beyond: Exception | None = None) -> None:
        self._script = list(script)
        self._beyond = beyond
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self._script:
            return self._script.pop(0)
        if self._beyond is not None:
            raise self._beyond
        raise AssertionError("the loop made an unscripted provider call")


__all__ = [
    "ANTHROPIC",
    "LATER",
    "LEASE",
    "MODEL_NAME",
    "NOW",
    "OWNER_USER_ID",
    "LoopRig",
    "RecordingSleeper",
    "ScriptedCompletion",
    "TripwireEgressPolicy",
    "add_connection",
    "agent_instance_id",
    "build_rig",
    "claim_run",
    "connection_row",
    "definition_fingerprint",
    "definitions_for",
    "descriptor_named",
    "discover_connection",
    "event_types",
    "final_turn",
    "grant",
    "invocation_rows",
    "job_row",
    "load_run",
    "loopback_origin",
    "loopback_policy",
    "migrate_database",
    "no_wait",
    "operator_config",
    "permission",
    "run_row",
    "submit_run",
    "succeed_run",
    "tool_call",
    "tool_turn",
]
