"""D7 integrated Stage-D acceptance over MCP sources: the composition the repository never built.

Every journey here runs the shipped stack end to end. The subject is the seam no pre-D7 test
crossed: a ``tool_loop`` is handed to ``RunExecutor``, and each Run is driven through
``JobExecutionService.execute(claimed_attempt)``, so the routing decision in ``run_execution.py`` --
which shape a tool-enabled Run takes -- is exercised by construction. Discovery, the durable
``tool_definitions`` rows, the D2 grant, the real ``McpRegistrySynchronizer``, the frozen catalog,
the D5 gateway/source/executor, the official SDK transport, and D4's loop are all real; the only
substitutions are the seams production documents as such (a loopback egress policy, since production
refuses plain ``http://``, and a scripted completion, since no provider may be reached).

The fake server's in-process ``Ledger`` is the remote-side evidence: every count asserted below is
an exact integer, and a zero means no socket carried a call. Time is injected and never awaited.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import Tool
from nervos_core.application.mcp_connections import ConnectionStatus
from nervos_core.application.model_completion import (
    ModelRequest,
    ModelResponse,
    ToolResultTurn,
)
from nervos_core.application.tool_invocations import InvocationStatus
from nervos_core.domain.runs import RunStatus
from nervos_core.domain.tools import DefinitionStatus, ToolSourceKind, ToolSourceRef
from nervos_core.infrastructure.database.mcp_connections import (
    SqlAlchemyMcpConnectionPersistence,
)
from nervos_mcp.operator_config import McpOperatorConfig, StdioServerSpec
from nervos_mcp.policy.egress import StrictEgressPolicy, parse_endpoint

from ..support.egress import LoopbackEgressPolicy
from ..support.fake_mcp_server import (
    CANONICAL_INPUT_SCHEMAS,
    CANONICAL_OUTPUT_SCHEMA,
    LEDGER_PATH_ENV,
    READ_DOCUMENT,
    WRITE_DOCUMENT,
    FakeMcpServer,
    build_server,
    serve_streamable_http,
)
from ..support.loop_harness import (
    NOW,
    OWNER_USER_ID,
    ScriptedCompletion,
    add_connection,
    agent_instance_id,
    connection_row,
    definition_fingerprint,
    descriptor_named,
    discover_connection,
    final_turn,
    grant,
    invocation_rows,
    loopback_origin,
    loopback_policy,
    migrate_database,
    operator_config,
    permission,
    run_row,
    submit_run,
    tool_call,
    tool_turn,
)
from .stage_d_mcp_support import (
    add_stdio_connection,
    build_execution,
    builtin_descriptors,
    definition_status,
    execute_run,
    job_and_attempt_counts,
    record_stdio_processes,
    reviewed_fingerprint,
    subprocess_available,
    tool_events,
)

# The stdio child runs this module from the package root, exactly as the existing stdio suite does.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
STDIO_SERVER_KEY = "fake-docs"
STDIO_MODULE = "tests.support.fake_stdio_server"

# Tools the extended fake servers add. Each is deliberately outside the authoritative server's two
# tools so the extension changes exactly the one fact its journey needs.
UNSUPPORTED_TOOL = "legacy_document"
FLAKY_TOOL = "flaky_document"


class HookedCompletion:
    """A scripted completion that runs one hook just before answering its first turn.

    Catalog assembly happens before the first ``complete`` call and dispatch happens after it, so a
    hook here is the only place a journey can change durable state *between* the frozen catalog and
    the live check a call runs under. J-I and J-J both depend on that exact ordering: the tool is
    catalogued while eligible and refused at dispatch.
    """

    def __init__(self, *script: ModelResponse, before_first: Callable[[], Awaitable[None]]) -> None:
        self._before_first = before_first
        self._script = list(script)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            await self._before_first()
        if not self._script:
            raise AssertionError("the loop made an unscripted provider call")
        return self._script.pop(0)


class DriftingFakeServer(FakeMcpServer):
    """The same two tools, able to advertise a materially different ``write_document``.

    Flipping :attr:`drifted` changes the description, which is part of the fingerprint the durable
    definition carries, so a rediscovery updates the definition in place while the grant still
    reviews the old bytes -- the drift D2 must refuse.
    """

    def __init__(self) -> None:
        super().__init__()
        self.drifted = False

    async def list_tools(self) -> list[Tool]:
        tools = await super().list_tools()
        if not self.drifted:
            return tools
        return [
            tool.model_copy(update={"description": f"{tool.description} (revised)"})
            if tool.name == WRITE_DOCUMENT
            else tool
            for tool in tools
        ]


class UnsupportedSchemaFakeServer(FakeMcpServer):
    """The fake server plus one tool whose SDK-derived schema is outside the canonical subset.

    The authoritative server rewrites its two tools' schemas into canonical form; this tool is left
    with the derived schema (which carries ``title`` keywords), so discovery persists it as
    ``unsupported_schema`` -- the D5 status that is stored and auditable but never offered.
    """

    def __init__(self) -> None:
        super().__init__()

        @self.tool(name=UNSUPPORTED_TOOL, description="A tool whose schema NervOS cannot admit.")
        def legacy_document(path: str) -> str:  # pyright: ignore[reportUnusedFunction]
            return f"legacy:{path}"


class FailingFakeServer(FakeMcpServer):
    """The fake server plus one canonical tool whose handler reports a known failure.

    Extending the authoritative server keeps the discovery surface, the ledger and the canonical
    schemas identical; only this one tool's behaviour differs, which is the single fact a
    multi-source journey needs a source to fail on.
    """

    def __init__(self) -> None:
        super().__init__()
        ledger = self.ledger

        @self.tool(name=FLAKY_TOOL, description="Record the call, then report a known failure.")
        def flaky_document(path: str) -> str:  # pyright: ignore[reportUnusedFunction]
            ledger.record(FLAKY_TOOL, path)
            raise ToolError("this tool always fails")

    async def list_tools(self) -> list[Tool]:
        tools = await super().list_tools()
        return [
            tool.model_copy(
                update={
                    "input_schema": CANONICAL_INPUT_SCHEMAS[READ_DOCUMENT],
                    "output_schema": CANONICAL_OUTPUT_SCHEMA,
                }
            )
            if tool.name == FLAKY_TOOL
            else tool
            for tool in tools
        ]


def _stdio_spec(ledger_path: Path) -> StdioServerSpec:
    """The operator's declaration of the stdio child: an executable, an argv, and no shell."""
    return StdioServerSpec(
        server_key=STDIO_SERVER_KEY,
        executable=sys.executable,
        args=("-m", STDIO_MODULE),
        working_dir=str(PACKAGE_ROOT),
        env={"PYTHONPATH": str(PACKAGE_ROOT), LEDGER_PATH_ENV: str(ledger_path)},
    )


@pytest.mark.anyio
async def test_journey_d_streamable_http_runs_end_to_end_through_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connection -> discovery -> durable definition -> grant -> Run -> service -> gateway -> call.

    This is the journey J-D exists for: a real ``tools/call`` over Streamable HTTP, dispatched by
    the D4 loop that ``JobExecutionService`` selected, with the durable invocation lifecycle and the
    three ``tool.*`` events as the evidence.
    """
    engine = migrate_database(tmp_path, monkeypatch)
    instance_id = agent_instance_id(engine)
    server = build_server()
    async with serve_streamable_http(server) as endpoint:
        config = operator_config(endpoint)
        policy = loopback_policy(endpoint)
        connection_id, descriptors = await add_connection(
            engine, endpoint=endpoint, config=config, policy=policy
        )
        write = descriptor_named(descriptors, WRITE_DOCUMENT)
        assert write.tool_definition_id is not None
        grant(engine, tool_definition_id=write.tool_definition_id, instance_id=instance_id)

        run_id = submit_run(engine, instance_id=instance_id)
        completion = ScriptedCompletion(
            tool_turn(tool_call("call-1", write.model_name, {"path": "a.txt", "content": "one"})),
            final_turn("wrote a.txt"),
        )
        stage = build_execution(engine, config=config, policy=policy, completion=completion)
        try:
            outcome = await execute_run(stage, run_id)
        finally:
            await stage.gateway.close_all()

        assert outcome is not None
        assert outcome.status == "succeeded"
        assert outcome.output_text == "wrote a.txt"
        assert run_row(engine, run_id)["status"] == RunStatus.SUCCEEDED.value
        assert job_and_attempt_counts(engine, run_id) == (1, 1, 1)

        rows = invocation_rows(engine)
        assert len(rows) == 1
        invocation = rows[0]
        assert invocation["status"] == InvocationStatus.SUCCEEDED.value
        assert invocation["tool_definition_id"] == write.tool_definition_id
        assert invocation["source_id"] == connection_id
        assert tool_events(engine, run_id) == [
            ("tool.requested", invocation["id"], None),
            ("tool.started", invocation["id"], None),
            ("tool.succeeded", invocation["id"], None),
        ]
        # Exactly one remote call, and the observation reached the model.
        assert server.ledger.entries == [f"{WRITE_DOCUMENT}:a.txt=one"]
        observation = completion.requests[1].turns[1]
        assert isinstance(observation, ToolResultTurn)
        assert observation.is_error is False


@pytest.mark.skipif(
    not subprocess_available(), reason="this platform cannot start a Python subprocess"
)
@pytest.mark.anyio
async def test_journey_e_operator_server_key_runs_one_real_stdio_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One operator-declared ``server_key`` -> a real child -> discovery + one call -> teardown.

    Representative rather than a matrix: the point is that ``server_key`` resolves to an
    operator-declared argv (never a shell string), that the official SDK drives a real subprocess,
    and that the child is gone afterwards. The captured handle is the SDK's own process object, so
    the exit assertion is about the real child rather than a proxy for it.
    """
    engine = migrate_database(tmp_path, monkeypatch)
    instance_id = agent_instance_id(engine)
    ledger_path = tmp_path / "ledger.txt"
    spec = _stdio_spec(ledger_path)
    config = McpOperatorConfig(stdio_servers={STDIO_SERVER_KEY: spec})
    policy = StrictEgressPolicy(frozenset())

    connection_id, descriptors = await add_stdio_connection(
        engine, server_key=STDIO_SERVER_KEY, config=config, policy=policy
    )
    read = descriptor_named(descriptors, READ_DOCUMENT)
    assert read.tool_definition_id is not None
    grant(engine, tool_definition_id=read.tool_definition_id, instance_id=instance_id)

    run_id = submit_run(engine, instance_id=instance_id)
    completion = ScriptedCompletion(
        tool_turn(tool_call("call-1", read.model_name, {"path": "a.txt"})),
        final_turn("read a.txt"),
    )
    spawned = record_stdio_processes(monkeypatch)
    stage = build_execution(engine, config=config, policy=policy, completion=completion)
    try:
        outcome = await execute_run(stage, run_id)
        assert outcome is not None and outcome.status == "succeeded"
        assert job_and_attempt_counts(engine, run_id) == (1, 1, 1)
        rows = invocation_rows(engine)
        assert len(rows) == 1
        assert rows[0]["status"] == InvocationStatus.SUCCEEDED.value
        assert rows[0]["source_id"] == connection_id
        assert ledger_path.read_text(encoding="utf-8").splitlines() == [f"{READ_DOCUMENT}:a.txt"]
    finally:
        await stage.gateway.close_all()

    # One child was spawned, and the SDK's bounded teardown reaped it: a live handle has no
    # return code, so a non-null code is the proof that no child process leaked.
    assert len(spawned) == 1
    assert spawned[0].returncode is not None


@pytest.mark.anyio
async def test_journey_c_builtin_and_two_mcp_sources_one_run_one_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One Attempt draws on the built-in source and two separate MCP connections at once.

    The catalog is a view over every registered source, so this proves the multi-source case is real
    rather than an aggregate fiction: descriptors keep their own source, calls execute in provider
    order, a grant for one connection authorizes nothing on the other, and one source failing does
    not disturb another's durable row.
    """
    engine = migrate_database(tmp_path, monkeypatch)
    instance_id = agent_instance_id(engine)
    server_a = build_server()
    server_b = FailingFakeServer()
    async with (
        serve_streamable_http(server_a) as endpoint_a,
        serve_streamable_http(server_b) as endpoint_b,
    ):
        # One gateway must be allowed to dial both loopback origins, so the operator config and
        # the egress policy list both -- no aggregate source identity is invented anywhere.
        config = McpOperatorConfig(
            allowed_origins=frozenset({loopback_origin(endpoint_a), loopback_origin(endpoint_b)})
        )
        policy = LoopbackEgressPolicy(
            {parse_endpoint(endpoint_a)[2], parse_endpoint(endpoint_b)[2]}
        )
        connection_a, descriptors_a = await add_connection(
            engine, endpoint=endpoint_a, config=config, policy=policy, display_name="A"
        )
        connection_b, descriptors_b = await add_connection(
            engine, endpoint=endpoint_b, config=config, policy=policy, display_name="B"
        )
        read_a = descriptor_named(descriptors_a, READ_DOCUMENT)
        read_b = descriptor_named(descriptors_b, READ_DOCUMENT)
        flaky_b = descriptor_named(descriptors_b, FLAKY_TOOL)
        calculate = descriptor_named(builtin_descriptors(engine), "calculate")
        for descriptor in (read_a, flaky_b, calculate):
            assert descriptor.tool_definition_id is not None
            grant(
                engine,
                tool_definition_id=descriptor.tool_definition_id,
                instance_id=instance_id,
            )

        run_id = submit_run(engine, instance_id=instance_id)
        completion = ScriptedCompletion(
            tool_turn(
                tool_call("call-a", read_a.model_name, {"path": "a.txt"}),
                tool_call("call-b", flaky_b.model_name, {"path": "b.txt"}),
                tool_call("call-builtin", calculate.model_name, {"expression": "1 + 1"}),
                # B's *other* tool is not granted, and A's grant reaches nothing on B: naming it
                # must not reach B at all.
                tool_call("call-b-read", read_b.model_name, {"path": "b.txt"}),
            ),
            final_turn("done"),
        )
        stage = build_execution(
            engine, config=config, policy=policy, builtins=True, completion=completion
        )
        try:
            outcome = await execute_run(stage, run_id)
        finally:
            await stage.gateway.close_all()

        offered = {schema.name for schema in completion.requests[0].tools}
        assert offered == {read_a.model_name, flaky_b.model_name, calculate.model_name}
        assert read_b.model_name not in offered

        rows = invocation_rows(engine)
        assert [row["status"] for row in rows] == [
            InvocationStatus.SUCCEEDED.value,
            InvocationStatus.FAILED.value,
            InvocationStatus.SUCCEEDED.value,
        ]
        assert rows[0]["source_kind"] == ToolSourceKind.MCP.value
        assert rows[0]["source_id"] == connection_a
        assert rows[0]["tool_definition_id"] == read_a.tool_definition_id
        assert rows[1]["source_kind"] == ToolSourceKind.MCP.value
        assert rows[1]["source_id"] == connection_b
        assert rows[1]["tool_definition_id"] == flaky_b.tool_definition_id
        assert rows[2]["source_kind"] == ToolSourceKind.BUILTIN.value
        assert rows[2]["tool_definition_id"] == calculate.tool_definition_id

        # Each invocation's own audit triple points at its own durable row, and the ungranted
        # call is a single generic refusal that never became a row.
        by_invocation: dict[int, list[str]] = {}
        refusals = 0
        for event_type, invocation_id, code in tool_events(engine, run_id):
            if invocation_id is None:
                assert event_type == "tool.denied" and code == "tool_denied"
                refusals += 1
                continue
            by_invocation.setdefault(int(invocation_id), []).append(event_type)
        assert by_invocation == {
            int(rows[0]["id"]): ["tool.requested", "tool.started", "tool.succeeded"],
            int(rows[1]["id"]): ["tool.requested", "tool.started", "tool.failed"],
            int(rows[2]["id"]): ["tool.requested", "tool.started", "tool.succeeded"],
        }
        assert refusals == 1

        # The failure was B's, and only B's: A and the built-in recorded exactly their one call.
        assert server_a.ledger.counts == {READ_DOCUMENT: 1}
        assert server_b.ledger.counts == {FLAKY_TOOL: 1}
        assert outcome is not None and outcome.status == "succeeded"
        assert job_and_attempt_counts(engine, run_id) == (1, 1, 1)


@pytest.mark.anyio
async def test_journey_i_definition_drift_denies_the_next_live_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fingerprint the user did not review suspends authority on the very next call.

    Rediscovery runs between catalog assembly and dispatch, so the tool is offered from reviewed
    material and still refused from live material -- the read that proves the catalog is not
    authority.
    """
    engine = migrate_database(tmp_path, monkeypatch)
    instance_id = agent_instance_id(engine)
    server = DriftingFakeServer()
    async with serve_streamable_http(server) as endpoint:
        config = operator_config(endpoint)
        policy = loopback_policy(endpoint)
        connection_id, descriptors = await add_connection(
            engine, endpoint=endpoint, config=config, policy=policy
        )
        write = descriptor_named(descriptors, WRITE_DOCUMENT)
        assert write.tool_definition_id is not None
        grant(engine, tool_definition_id=write.tool_definition_id, instance_id=instance_id)
        reviewed = reviewed_fingerprint(
            engine, instance_id=instance_id, tool_definition_id=write.tool_definition_id
        )
        assert reviewed == write.fingerprint

        async def drift() -> None:
            server.drifted = True
            await discover_connection(
                engine, connection_id=connection_id, config=config, policy=policy
            )

        run_id = submit_run(engine, instance_id=instance_id)
        completion = HookedCompletion(
            tool_turn(tool_call("call-1", write.model_name, {"path": "a.txt", "content": "one"})),
            final_turn("drifted"),
            before_first=drift,
        )
        stage = build_execution(engine, config=config, policy=policy, completion=completion)
        try:
            outcome = await execute_run(stage, run_id)
        finally:
            await stage.gateway.close_all()

        # The definition really moved, and the grant really did not.
        assert definition_fingerprint(engine, write.tool_definition_id) != reviewed
        assert (
            reviewed_fingerprint(
                engine, instance_id=instance_id, tool_definition_id=write.tool_definition_id
            )
            == reviewed
        )

        rows = invocation_rows(engine)
        assert len(rows) == 1
        assert rows[0]["status"] == InvocationStatus.DENIED.value
        assert rows[0]["permission_decision"] == "denied_definition_changed"
        assert tool_events(engine, run_id) == [
            ("tool.requested", rows[0]["id"], None),
            ("tool.denied", rows[0]["id"], "tool_denied"),
        ]
        assert "tool.started" not in [event[0] for event in tool_events(engine, run_id)]
        # D2 denied before a socket existed, so the server saw no call at all.
        assert server.ledger.counts == {}
        assert outcome is not None and outcome.output_text == "drifted"


@pytest.mark.anyio
async def test_journey_i2_unsupported_schema_is_not_offered_and_never_reaches_the_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A definition whose schema NervOS cannot admit is stored, excluded, and refused.

    Frozen D5 behaviour is that ``unsupported_schema`` is durable and auditable but never offered by
    ``McpToolSource``. So a model naming it is a catalog miss -- a generic pre-dispatch refusal with
    no invocation row, never a D2 definition-unavailable denial -- and no descriptor is invented to
    force a different shape.
    """
    engine = migrate_database(tmp_path, monkeypatch)
    instance_id = agent_instance_id(engine)
    server = UnsupportedSchemaFakeServer()
    async with serve_streamable_http(server) as endpoint:
        config = operator_config(endpoint)
        policy = loopback_policy(endpoint)
        _connection_id, descriptors = await add_connection(
            engine, endpoint=endpoint, config=config, policy=policy
        )
        legacy = descriptor_named(descriptors, UNSUPPORTED_TOOL)
        read = descriptor_named(descriptors, READ_DOCUMENT)
        assert legacy.tool_definition_id is not None
        assert read.tool_definition_id is not None

        status = definition_status(engine, legacy.tool_definition_id)
        assert status == DefinitionStatus.UNSUPPORTED_SCHEMA.value
        # The unsupported definition is not offered; an admissible sibling is, so the exclusion is
        # about the schema rather than about an empty catalog.
        grant(engine, tool_definition_id=read.tool_definition_id, instance_id=instance_id)

        run_id = submit_run(engine, instance_id=instance_id)
        completion = ScriptedCompletion(
            tool_turn(tool_call("call-1", legacy.model_name, {"path": "legacy.txt"})),
            final_turn("refused"),
        )
        stage = build_execution(engine, config=config, policy=policy, completion=completion)
        try:
            outcome = await execute_run(stage, run_id)
        finally:
            await stage.gateway.close_all()

        offered = {schema.name for schema in completion.requests[0].tools}
        assert offered == {read.model_name}
        assert legacy.model_name not in offered

        assert invocation_rows(engine) == []
        assert tool_events(engine, run_id) == [("tool.denied", None, "tool_denied")]
        # Nothing reached the executor: no socket carried a call for the unsupported tool.
        assert server.ledger.counts == {}
        assert outcome is not None and outcome.status == "succeeded"


@pytest.mark.anyio
async def test_journey_j_disable_after_catalog_still_denies_in_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection disabled after the catalog was built denies the already-catalogued tool.

    The ordering is the point: the tool is eligible at assembly, so the frozen catalog still offers
    it, and the live check at dispatch is what refuses it. A stale Worker registration must not
    confer authority, and re-enabling alone must not make the stale definition executable -- a
    successful refresh is required.
    """
    engine = migrate_database(tmp_path, monkeypatch)
    instance_id = agent_instance_id(engine)
    server = build_server()
    connections = SqlAlchemyMcpConnectionPersistence(engine)
    async with serve_streamable_http(server) as endpoint:
        config = operator_config(endpoint)
        policy = loopback_policy(endpoint)
        connection_id, descriptors = await add_connection(
            engine, endpoint=endpoint, config=config, policy=policy
        )
        write = descriptor_named(descriptors, WRITE_DOCUMENT)
        assert write.tool_definition_id is not None
        grant(engine, tool_definition_id=write.tool_definition_id, instance_id=instance_id)

        async def disable() -> None:
            assert (
                connections.set_disabled(
                    connection_id=connection_id, owner_user_id=OWNER_USER_ID, now=NOW
                )
                is True
            )

        run_id = submit_run(engine, instance_id=instance_id)
        completion = HookedCompletion(
            tool_turn(tool_call("call-1", write.model_name, {"path": "a.txt", "content": "one"})),
            final_turn("denied"),
            before_first=disable,
        )
        stage = build_execution(engine, config=config, policy=policy, completion=completion)
        try:
            outcome = await execute_run(stage, run_id)
        finally:
            await stage.gateway.close_all()

        # The tool was offered (the catalog was assembled while eligible) ...
        assert write.model_name in {schema.name for schema in completion.requests[0].tools}
        # ... and the live check refused it: requested -> denied, never started.
        rows = invocation_rows(engine)
        assert len(rows) == 1
        assert rows[0]["status"] == InvocationStatus.DENIED.value
        assert rows[0]["permission_decision"] == "denied_connection_disabled"
        assert tool_events(engine, run_id) == [
            ("tool.requested", rows[0]["id"], None),
            ("tool.denied", rows[0]["id"], "tool_denied"),
        ]
        assert server.ledger.counts == {}
        assert outcome is not None and outcome.status == "succeeded"

        # A stale registration is still present in this process, and it conferred nothing.
        assert ToolSourceRef(ToolSourceKind.MCP, connection_id) in set(stage.registry.source_refs())
        assert permission(engine, run_id=run_id, tool_definition_id=write.tool_definition_id) == (
            False,
            "CONNECTION_DISABLED",
        )

        # Re-enabling alone is not a refresh: the catalog returns to `needs_refresh` and every
        # definition is marked unavailable, so the stale definition is still not executable.
        assert (
            connections.set_reenabled(
                connection_id=connection_id, owner_user_id=OWNER_USER_ID, now=NOW
            )
            is True
        )
        assert (
            connection_row(engine, connection_id).catalog_status is ConnectionStatus.NEEDS_REFRESH
        )
        assert permission(engine, run_id=run_id, tool_definition_id=write.tool_definition_id) == (
            False,
            "DEFINITION_UNAVAILABLE",
        )
