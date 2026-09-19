"""Blocker A: a Worker's tool registry is synchronised from durable state before each Attempt.

The subject is the seam D4 calls once per Attempt. Every test here drives the **real**
:class:`~nervos_worker.mcp.McpRegistrySynchronizer`, the real durable connection persistence, the
real D2 evaluator, and (where a call would otherwise happen) the real gateway, source, and executor
over a live fake MCP server. Nothing stands in for the sync path, the discovery path, or the
permission layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nervos_core.application.builtin_tools import BUILTIN_SOURCE_REF
from nervos_core.application.mcp_connections import ConnectionTransport
from nervos_core.application.tool_catalog import gather_descriptors
from nervos_core.application.tool_invocations import InvocationStatus
from nervos_core.domain.runs import RunStatus
from nervos_core.domain.tools import ToolSourceKind, ToolSourceRef
from nervos_core.infrastructure.database.mcp_connections import SqlAlchemyMcpConnectionPersistence
from nervos_mcp.operator_config import McpOperatorConfig
from sqlalchemy import text

from ..support.fake_mcp_server import (
    READ_DOCUMENT,
    WRITE_DOCUMENT,
    build_server,
    serve_streamable_http,
)
from ..support.loop_harness import (
    LATER,
    NOW,
    ScriptedCompletion,
    TripwireEgressPolicy,
    add_connection,
    agent_instance_id,
    build_rig,
    claim_run,
    definitions_for,
    descriptor_named,
    final_turn,
    grant,
    invocation_rows,
    load_run,
    loopback_policy,
    migrate_database,
    operator_config,
    permission,
    submit_run,
    succeed_run,
    tool_call,
    tool_turn,
)


@pytest.mark.anyio
async def test_a_connection_discovered_after_startup_is_executable_without_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry and loop are built against an empty world; the connection arrives afterwards."""
    engine = migrate_database(tmp_path, monkeypatch)
    instance_id = agent_instance_id(engine)
    server = build_server()
    async with serve_streamable_http(server) as endpoint:
        config = operator_config(endpoint)
        policy = loopback_policy(endpoint)
        # Built first, while the database has no MCP connection at all.
        rig = build_rig(engine, config=config, policy=policy)
        assert rig.registry.source_refs() == ()
        try:
            connection_id, descriptors = await add_connection(
                engine, endpoint=endpoint, config=config, policy=policy
            )
            read = descriptor_named(descriptors, READ_DOCUMENT)
            grant(engine, tool_definition_id=read.tool_definition_id, instance_id=instance_id)
            run_id = submit_run(engine, instance_id=instance_id)
            _, claim = claim_run(engine, run_id)
            run = load_run(engine, run_id)

            completion = ScriptedCompletion(
                tool_turn(tool_call("call-1", read.model_name, {"path": "a.txt"})),
                final_turn("read it"),
            )
            # The SAME loop instance built before the connection existed.
            outcome = await rig.loop.run(completion, run, claim, 0)
            assert succeed_run(engine, claim, outcome) is True

            # The tool reached the Attempt catalog without any restart: offered, then executed.
            assert read.model_name in {schema.name for schema in completion.requests[0].tools}
            rows = invocation_rows(engine)
            assert len(rows) == 1
            assert rows[0]["status"] == InvocationStatus.SUCCEEDED.value
            assert rows[0]["tool_definition_id"] == read.tool_definition_id
            assert rows[0]["source_id"] == connection_id
            assert server.ledger.counts[READ_DOCUMENT] == 1
            assert load_run(engine, run_id).status is RunStatus.SUCCEEDED
        finally:
            await rig.gateway.close_all()


@pytest.mark.anyio
async def test_two_mcp_connections_and_a_builtin_share_one_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One Attempt's catalog is a view over every registered source, not over one connection."""
    engine = migrate_database(tmp_path, monkeypatch)
    instance_id = agent_instance_id(engine)
    server = build_server()
    async with serve_streamable_http(server) as endpoint:
        config = operator_config(endpoint)
        policy = loopback_policy(endpoint)

        # Two durable connections to the same server are two distinct sources, so this one Attempt
        # leaves two live gateway sessions -- the case the shutdown path has to unwind correctly.
        rig = build_rig(engine, config=config, policy=policy, builtins=True)
        try:
            # Two durable connections to the same server are two distinct sources: their model
            # names are namespaced by connection id, so both definitions coexist and both execute.
            _, descriptors_a = await add_connection(
                engine, endpoint=endpoint, config=config, policy=policy, display_name="Docs A"
            )
            _, descriptors_b = await add_connection(
                engine, endpoint=endpoint, config=config, policy=policy, display_name="Docs B"
            )
            read_a = descriptor_named(descriptors_a, READ_DOCUMENT)
            write_b = descriptor_named(descriptors_b, WRITE_DOCUMENT)
            builtin = descriptor_named(definitions_for(engine, BUILTIN_SOURCE_REF), "calculate")
            assert read_a.tool_definition_id != write_b.tool_definition_id

            for descriptor in (read_a, write_b, builtin):
                grant(
                    engine,
                    tool_definition_id=descriptor.tool_definition_id,
                    instance_id=instance_id,
                )

            run_id = submit_run(engine, instance_id=instance_id)
            _, claim = claim_run(engine, run_id)
            run = load_run(engine, run_id)
            # One call from each of the three sources, so all three execute in one Attempt.
            completion = ScriptedCompletion(
                tool_turn(tool_call("mcp-a", read_a.model_name, {"path": "a.txt"})),
                tool_turn(
                    tool_call("mcp-b", write_b.model_name, {"path": "b.txt", "content": "two"})
                ),
                tool_turn(tool_call("builtin", builtin.model_name, {"expression": "1/3"})),
                final_turn("all three"),
            )

            outcome = await rig.loop.run(completion, run, claim, 0)
            assert succeed_run(engine, claim, outcome) is True

            # The frozen catalog is a grant-filtered view over every registered source.
            offered = {schema.name for schema in completion.requests[0].tools}
            assert {read_a.model_name, write_b.model_name, builtin.model_name} <= offered
            # Gathering collects every source's descriptors; all three granted ones are present.
            gathered = await gather_descriptors(rig.registry)
            assert {
                read_a.tool_definition_id,
                write_b.tool_definition_id,
                builtin.tool_definition_id,
            } <= {descriptor.tool_definition_id for descriptor in gathered}

            rows = invocation_rows(engine)
            assert len(rows) == 3
            assert {row["tool_definition_id"] for row in rows} == {
                read_a.tool_definition_id,
                write_b.tool_definition_id,
                builtin.tool_definition_id,
            }
            assert all(row["status"] == InvocationStatus.SUCCEEDED.value for row in rows)
            assert server.ledger.counts[READ_DOCUMENT] == 1
            assert server.ledger.counts[WRITE_DOCUMENT] == 1
            assert outcome.output_text == "all three"

            # D6 correlation: three sources in one Attempt produce three *linked* tool timelines,
            # each event naming the invocation of the call it is about. This is the multi-source
            # proof -- the audit is source-neutral and one call cannot be attributed to another,
            # whether the source is a built-in or one of two connections to the same server.
            with engine.connect() as connection:
                linked = connection.execute(
                    text(
                        "SELECT event_type, tool_invocation_id FROM run_events"
                        " WHERE run_id = :r AND event_type LIKE 'tool.%' ORDER BY sequence"
                    ),
                    {"r": run_id},
                ).all()
            by_invocation: dict[int, list[str]] = {}
            for event_type, invocation_id in linked:
                assert invocation_id is not None
                by_invocation.setdefault(int(invocation_id), []).append(event_type)
            # Each invocation has its own complete, ordered triple, and the triples do not merge.
            assert len(by_invocation) == 3
            assert all(
                kinds == ["tool.requested", "tool.started", "tool.succeeded"]
                for kinds in by_invocation.values()
            )
            assert {int(row["id"]) for row in rows} == set(by_invocation)
        finally:
            # `close_all` is the real shutdown path, and this test leaves *two* live sessions in
            # the cache -- which is exactly the case that used to raise, because each client nests
            # an anyio cancel scope in this task and they must unwind last-in-first-out. A second
            # call proves the cache was drained rather than left half-closed.
            await rig.gateway.close_all()
            await rig.gateway.close_all()


@pytest.mark.anyio
async def test_synchronize_for_run_opens_no_network_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sync registers a source from durable rows alone; a never-served endpoint is never dialled."""
    engine = migrate_database(tmp_path, monkeypatch)
    instance_id = agent_instance_id(engine)
    # A contributing connection whose endpoint is never served, and a policy that fails loudly if
    # anything asks it to approve a dial.
    connection_id = SqlAlchemyMcpConnectionPersistence(engine).create(
        owner_user_id=1,
        display_name="Unreachable",
        transport=ConnectionTransport.HTTP,
        endpoint="http://127.0.0.1:9/mcp",
        server_key=None,
        credential_ref=None,
        now=NOW,
    )
    tripwire = TripwireEgressPolicy()
    rig = build_rig(engine, config=McpOperatorConfig(), policy=tripwire)
    try:
        run = load_run(engine, submit_run(engine, instance_id=instance_id))

        await rig.synchronizer.synchronize_for_run(run)

        assert tripwire.asked is False
        assert ToolSourceRef(ToolSourceKind.MCP, connection_id) in rig.registry.source_refs()
    finally:
        await rig.gateway.close_all()


@pytest.mark.anyio
async def test_a_stale_local_registration_is_not_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disabled connection's tool is denied from durable state, whatever this process cached."""
    engine = migrate_database(tmp_path, monkeypatch)
    instance_id = agent_instance_id(engine)
    server = build_server()
    async with serve_streamable_http(server) as endpoint:
        config = operator_config(endpoint)
        policy = loopback_policy(endpoint)
        connection_id, descriptors = await add_connection(
            engine, endpoint=endpoint, config=config, policy=policy
        )
        read = descriptor_named(descriptors, READ_DOCUMENT)
        grant(engine, tool_definition_id=read.tool_definition_id, instance_id=instance_id)

        rig = build_rig(engine, config=config, policy=policy)
        try:
            # Register locally, then disable the connection durably. No invalidation is broadcast.
            assert rig.synchronizer.synchronize() == 1
            source_ref = ToolSourceRef(ToolSourceKind.MCP, connection_id)
            assert source_ref in rig.registry.source_refs()
            assert (
                SqlAlchemyMcpConnectionPersistence(engine).set_disabled(
                    connection_id=connection_id, owner_user_id=1, now=NOW
                )
                is True
            )

            run_id = submit_run(engine, instance_id=instance_id)
            _, claim = claim_run(engine, run_id)
            run = load_run(engine, run_id)
            completion = ScriptedCompletion(
                tool_turn(tool_call("call-1", read.model_name, {"path": "a.txt"})),
                final_turn("it cannot"),
            )
            await rig.loop.run(completion, run, claim, 0)

            # Durable truth denies the call, and the stale registration simply stays put.
            allowed, _reason = permission(
                engine, run_id=run_id, tool_definition_id=read.tool_definition_id
            )
            assert allowed is False
            assert source_ref in rig.registry.source_refs()
            # The tool was never offered, so no invocation row exists and no server was reached.
            assert read.model_name not in {schema.name for schema in completion.requests[0].tools}
            assert invocation_rows(engine) == []
            assert server.ledger.counts.get(READ_DOCUMENT, 0) == 0
        finally:
            await rig.gateway.close_all()


@pytest.mark.anyio
async def test_a_grant_created_after_submission_stays_invisible_with_sync_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synchronising a live connection cannot widen a Run past its grant cutoff."""
    engine = migrate_database(tmp_path, monkeypatch)
    instance_id = agent_instance_id(engine)
    server = build_server()
    async with serve_streamable_http(server) as endpoint:
        config = operator_config(endpoint)
        policy = loopback_policy(endpoint)
        _connection_id, descriptors = await add_connection(
            engine, endpoint=endpoint, config=config, policy=policy
        )
        read = descriptor_named(descriptors, READ_DOCUMENT)
        rig = build_rig(engine, config=config, policy=policy)
        try:
            run_id = submit_run(engine, instance_id=instance_id)
            # The grant is created *after* submission, so the Run's cutoff excludes it.
            grant(
                engine,
                tool_definition_id=read.tool_definition_id,
                instance_id=instance_id,
                now=LATER,
            )
            _, claim = claim_run(engine, run_id)
            run = load_run(engine, run_id)
            completion = ScriptedCompletion(
                tool_turn(tool_call("call-1", read.model_name, {"path": "a.txt"})),
                final_turn("no tool"),
            )
            await rig.loop.run(completion, run, claim, 0)

            allowed, reason = permission(
                engine, run_id=run_id, tool_definition_id=read.tool_definition_id
            )
            assert allowed is False
            assert reason == "GRANT_AFTER_RUN_CUTOFF"
            assert read.model_name not in {schema.name for schema in completion.requests[0].tools}
            assert invocation_rows(engine) == []
            assert server.ledger.counts.get(READ_DOCUMENT, 0) == 0
        finally:
            await rig.gateway.close_all()
