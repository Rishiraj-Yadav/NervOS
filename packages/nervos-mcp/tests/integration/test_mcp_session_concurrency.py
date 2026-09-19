"""Is one cached MCP session safe for concurrent callers?

:class:`~nervos_mcp.gateway.McpGateway` caches exactly one live client per durable connection, and
its ``_session_for`` holds a lock only while *creating* that session -- never while
:meth:`~nervos_mcp.gateway.McpGateway.call_tool` is in flight. So when two callers reach the same
connection at once they overlap on one shared SDK ``Client`` and one transport.

That is not hypothetical: one Worker may run two Attempts against one connection, and nothing
serialises them. The question these tests answer empirically is whether the shipped gateway is
correct under that overlap -- whether each caller receives the response *it* asked for, and whether
the fake server's ledger records exactly the calls that were made -- or whether a crossed response,
an exception, or a reopen shows the cache needs a per-session lock around ``call_tool`` too.

The session is primed with one call before the concurrent burst, and sharing is then measured from
the server's side of the seam rather than from the gateway's private cache: every client the gateway
opens constructs exactly one transport, so counting constructions of the shipped ``http_transport``
factory says how many sessions really exist. A burst that quietly built a second client, or that
reopened one after a failed call, would show up as a second construction. The loop variant repeats
the burst to make an intermittent interleaving more likely to surface.

A third test covers the other half of the cache's contract -- a concurrent burst that arrives
*before* any session exists. That is where the same counting seam exposed a real defect: the
creation lock was constructed per call rather than per connection, so concurrent first-callers each
built a transport and all but the last were overwritten and leaked. The gateway now keeps one
stable lock per connection, and this test asserts the corrected behaviour -- exactly one session,
and a `close_all` that drains rather than raises.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from mcp.client import Transport
from nervos_core.infrastructure.database.mcp_connections import (
    SqlAlchemyMcpConnectionPersistence,
)
from nervos_mcp import gateway as gateway_module
from nervos_mcp.gateway import McpGateway
from nervos_mcp.operator_config import McpOperatorConfig, SecretValue
from nervos_mcp.policy.egress import EgressPolicy
from sqlalchemy import Engine

from ..support.fake_mcp_server import (
    READ_DOCUMENT,
    WRITE_DOCUMENT,
    FakeMcpServer,
    build_server,
    serve_streamable_http,
)
from ..support.loop_harness import (
    add_connection,
    loopback_policy,
    migrate_database,
    operator_config,
)

# A call is a tool name plus the arguments and the exact text the fake server answers with. The
# expected text is derived from the arguments, so a response delivered to the wrong caller is
# detectable rather than merely "something came back".
Call = tuple[str, dict[str, Any], str]


def read_call(path: str) -> Call:
    return (READ_DOCUMENT, {"path": path}, f"document:{path}")


def write_call(path: str, content: str) -> Call:
    return (WRITE_DOCUMENT, {"path": path, "content": content}, f"wrote:{path}")


def _build_gateway(
    engine: Engine, *, config: McpOperatorConfig, policy: EgressPolicy
) -> McpGateway:
    return McpGateway(
        connections=SqlAlchemyMcpConnectionPersistence(engine),
        operator=config,
        policy=policy,
    )


def _count_gateway_client_opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every transport the gateway builds, which is exactly one per live client.

    Wrapping the module-level factory is a public seam: the gateway imports ``http_transport`` by
    name and ``_open_client`` calls it once per client, so the recorded list is the number of
    sessions the gateway really opened -- without reaching into its private cache.
    """
    opened: list[str] = []
    real = gateway_module.http_transport

    def counting(
        endpoint: str,
        policy: EgressPolicy,
        *,
        credential: SecretValue | None = None,
    ) -> Transport:
        opened.append(endpoint)
        return real(endpoint, policy, credential=credential)

    monkeypatch.setattr(gateway_module, "http_transport", counting)
    return opened


async def _burst(gateway: McpGateway, connection_id: int, calls: list[Call]) -> list[Any]:
    """Fire every call at once and hand back results positionally, exceptions included."""
    return list(
        await asyncio.gather(
            *(
                gateway.call_tool(connection_id, name, arguments)
                for name, arguments, _expected in calls
            ),
            return_exceptions=True,
        )
    )


def _assert_each_caller_got_its_own_result(calls: list[Call], results: list[Any]) -> None:
    assert len(results) == len(calls)
    for (name, _arguments, expected), result in zip(calls, results, strict=True):
        assert not isinstance(result, BaseException), f"{name} raised under concurrency: {result!r}"
        assert result.is_error is False, f"{name} reported an error result: {result!r}"
        # The result must be the one *this* caller asked for, not merely a well-formed one.
        assert result.content[0].text == expected


def _assert_ledger_is_exact(server: FakeMcpServer, calls: list[Call]) -> None:
    reads = sum(1 for name, _args, _expected in calls if name == READ_DOCUMENT)
    writes = sum(1 for name, _args, _expected in calls if name == WRITE_DOCUMENT)
    assert server.ledger.counts.get(READ_DOCUMENT, 0) == reads
    assert server.ledger.counts.get(WRITE_DOCUMENT, 0) == writes
    # Order is not part of the claim under concurrency; the multiset of effects is.
    expected_entries = sorted(
        f"{name}:{_entry(arguments, name)}" for name, arguments, _expected in calls
    )
    assert sorted(server.ledger.entries) == expected_entries


def _entry(arguments: dict[str, Any], name: str) -> str:
    if name == WRITE_DOCUMENT:
        return f"{arguments['path']}={arguments['content']}"
    return str(arguments["path"])


@pytest.mark.anyio
async def test_one_cached_session_serves_concurrent_callers_without_crossing_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two callers overlap on one cached client and each still gets its own answer."""
    engine = migrate_database(tmp_path, monkeypatch)
    server = build_server()
    async with serve_streamable_http(server) as endpoint:
        config = operator_config(endpoint)
        policy = loopback_policy(endpoint)
        connection_id, _descriptors = await add_connection(
            engine, endpoint=endpoint, config=config, policy=policy
        )
        opened = _count_gateway_client_opens(monkeypatch)
        gateway = _build_gateway(engine, config=config, policy=policy)
        try:
            # Prime the cache: the burst below must reuse this exact client, not build another.
            await gateway.call_tool(connection_id, READ_DOCUMENT, {"path": "warmup.txt"})
            assert opened == [endpoint]

            calls = [
                read_call("a.txt"),
                write_call("b.txt", "two"),
                read_call("c.txt"),
                write_call("d.txt", "four"),
            ]
            results = await _burst(gateway, connection_id, calls)

            _assert_each_caller_got_its_own_result(calls, results)
            # The warm-up plus the burst is exactly what the server saw.
            _assert_ledger_is_exact(server, [read_call("warmup.txt"), *calls])
            # Sharing is proven, not assumed: the whole concurrent burst traversed the one client
            # the gateway opened. A second construction would mean a second session was built.
            assert opened == [endpoint]
        finally:
            await gateway.close_all()


@pytest.mark.anyio
async def test_one_cached_session_survives_repeated_concurrent_bursts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A five-call loop variant, to make an intermittent interleaving more likely to show."""
    engine = migrate_database(tmp_path, monkeypatch)
    server = build_server()
    async with serve_streamable_http(server) as endpoint:
        config = operator_config(endpoint)
        policy = loopback_policy(endpoint)
        connection_id, _descriptors = await add_connection(
            engine, endpoint=endpoint, config=config, policy=policy
        )
        opened = _count_gateway_client_opens(monkeypatch)
        gateway = _build_gateway(engine, config=config, policy=policy)
        try:
            await gateway.call_tool(connection_id, READ_DOCUMENT, {"path": "warmup.txt"})

            rounds = [
                [
                    read_call("r0.txt"),
                    write_call("r1.txt", "one"),
                    read_call("r2.txt"),
                    write_call("r3.txt", "three"),
                    read_call("r4.txt"),
                ],
                [
                    write_call("s0.txt", "zero"),
                    read_call("s1.txt"),
                    read_call("s2.txt"),
                    write_call("s3.txt", "three"),
                    read_call("s4.txt"),
                ],
                [
                    read_call("t0.txt"),
                    read_call("t1.txt"),
                    write_call("t2.txt", "two"),
                    read_call("t3.txt"),
                    write_call("t4.txt", "four"),
                ],
                [
                    write_call("u0.txt", "zero"),
                    write_call("u1.txt", "one"),
                    read_call("u2.txt"),
                    read_call("u3.txt"),
                    read_call("u4.txt"),
                ],
                [
                    read_call("v0.txt"),
                    write_call("v1.txt", "one"),
                    read_call("v2.txt"),
                    write_call("v3.txt", "three"),
                    read_call("v4.txt"),
                ],
            ]
            all_calls = [read_call("warmup.txt")]
            for calls in rounds:
                results = await _burst(gateway, connection_id, calls)
                _assert_each_caller_got_its_own_result(calls, results)
                all_calls.extend(calls)

            _assert_ledger_is_exact(server, all_calls)
            # One client for the warm-up and all five bursts: no call failed and forced a reopen.
            assert opened == [endpoint]
        finally:
            await gateway.close_all()


@pytest.mark.anyio
async def test_concurrent_cold_callers_share_one_created_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first callers to a cached-but-empty connection must not each build a session.

    This is the cold-start counterpart of the tests above. A concurrent burst that arrives with
    nothing cached must still construct exactly **one** transport: the gateway keeps one stable
    creation lock per connection, so the first caller to hold it creates the session and every
    other caller finds it cached. More than one construction means each racing caller built a
    private session, and all but the last were overwritten in the cache and leaked -- which is also
    what made the shutdown path raise, because the survivor had been entered inside a gather child
    task and could not be exited from this one.
    """
    engine = migrate_database(tmp_path, monkeypatch)
    server = build_server()
    async with serve_streamable_http(server) as endpoint:
        config = operator_config(endpoint)
        policy = loopback_policy(endpoint)
        connection_id, _descriptors = await add_connection(
            engine, endpoint=endpoint, config=config, policy=policy
        )
        opened = _count_gateway_client_opens(monkeypatch)
        gateway = _build_gateway(engine, config=config, policy=policy)
        # No warm-up: the burst itself is what must populate the cache, exactly once.
        calls = [
            read_call("a.txt"),
            write_call("b.txt", "two"),
            read_call("c.txt"),
            write_call("d.txt", "four"),
            read_call("e.txt"),
        ]
        results = await _burst(gateway, connection_id, calls)

        # Exactly one session, whatever the interleaving: one transport construction, and one
        # constructor call -- so no caller built a private client it then threw away.
        assert opened == [endpoint]
        # Every caller still gets the answer *it* asked for, over that one shared session.
        _assert_each_caller_got_its_own_result(calls, results)
        _assert_ledger_is_exact(server, calls)

        # Sharing, proved without reading the gateway's private cache: a second call after the
        # burst reuses the cached session rather than opening another.
        await gateway.call_tool(connection_id, READ_DOCUMENT, {"path": "again.txt"})
        assert opened == [endpoint]

        # The shutdown path now succeeds: the surviving session was entered under the creation
        # lock in this task, so its cancel scope can be exited here. A second `close_all` proves
        # the cache was drained rather than left half-closed, and no session was leaked.
        await gateway.close_all()
        assert opened == [endpoint]
        await gateway.close_all()


async def _fresh_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, server: FakeMcpServer, endpoint: str
) -> tuple[Engine, McpGateway, int, list[str]]:
    """A gateway with one registered connection and client-open counting already installed."""
    engine = migrate_database(tmp_path, monkeypatch)
    config = operator_config(endpoint)
    policy = loopback_policy(endpoint)
    connection_id, _descriptors = await add_connection(
        engine, endpoint=endpoint, config=config, policy=policy
    )
    opened = _count_gateway_client_opens(monkeypatch)
    return engine, _build_gateway(engine, config=config, policy=policy), connection_id, opened


def _lifecycle_owners() -> list[asyncio.Task[Any]]:
    """The gateway's per-connection lifecycle tasks, identified by the coroutine they run.

    Identified this way rather than by reading the gateway's cache: what matters is that a task
    exists to own the client context and that shutdown leaves none behind, and that is a property
    of the running loop. Filtering on the coroutine also keeps the fake HTTP server's own tasks --
    which are not gateway lifecycle at all -- out of the count.
    """
    return [
        task
        for task in asyncio.all_tasks()
        if "_own_session" in (getattr(task.get_coro(), "__qualname__", "") or "")
    ]


@pytest.mark.anyio
async def test_a_session_created_in_an_attempt_task_is_closed_by_worker_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production shape: an Attempt creates the session, the Worker's task closes it.

    This is the defect the owner task exists for. An anyio cancel scope belongs to the task that
    entered it, and the Worker runs each Attempt with ``asyncio.create_task`` while closing MCP
    sessions from its own long-lived shutdown task. Before the owner task, that mismatch made
    shutdown raise for any Worker that had run a single MCP call.
    """
    server = build_server()
    async with serve_streamable_http(server) as endpoint:
        _engine, gateway, connection_id, opened = await _fresh_gateway(
            tmp_path, monkeypatch, server=server, endpoint=endpoint
        )
        assert _lifecycle_owners() == []

        # Exactly what JobExecutionService does: one Attempt, in its own task, finishing before
        # shutdown ever begins.
        attempt = asyncio.create_task(
            gateway.call_tool(connection_id, READ_DOCUMENT, {"path": "a.txt"})
        )
        result = await attempt
        assert result.is_error is False
        assert len(_lifecycle_owners()) == 1

        # The Worker's shutdown path, running in this (different) task. It must succeed, and it
        # must leave no lifecycle owner behind.
        await gateway.close_all()
        assert _lifecycle_owners() == [], "close_all left a lifecycle owner running"
        assert opened == [endpoint]
        # And the same shutdown path is safe to run twice.
        await gateway.close_all()
        assert _lifecycle_owners() == []


@pytest.mark.anyio
async def test_invalidate_from_a_different_task_closes_through_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalidation is cross-task safe, and the connection is reusable afterwards.

    A connection can be disabled or deleted while an Attempt is in flight, so the invalidation
    runs in whatever task the caller happens to be in -- never necessarily the one that built the
    client.
    """
    server = build_server()
    async with serve_streamable_http(server) as endpoint:
        _engine, gateway, connection_id, opened = await _fresh_gateway(
            tmp_path, monkeypatch, server=server, endpoint=endpoint
        )
        attempt = asyncio.create_task(
            gateway.call_tool(connection_id, READ_DOCUMENT, {"path": "a.txt"})
        )
        await attempt

        # Called from this task, which did not create the session.
        await gateway.invalidate(connection_id)

        # The connection still works, and it rebuilt exactly one fresh session.
        again = await gateway.call_tool(connection_id, READ_DOCUMENT, {"path": "b.txt"})
        assert again.is_error is False
        assert opened == [endpoint, endpoint]

        await gateway.close_all()
        assert opened == [endpoint, endpoint]
