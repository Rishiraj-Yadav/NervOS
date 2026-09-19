"""Blocker B: real MCP execution through the D4 loop, over Streamable HTTP, end to end.

Every journey here runs the shipped stack: discovery over a live loopback MCP server, the durable
``tool_definitions`` row it produces, a real D2 grant, the real registry synchroniser, the real
catalog, the real :class:`~nervos_mcp.executor.McpToolExecutor`, the real
:class:`~nervos_mcp.gateway.McpGateway`, and the official SDK's ``tools/call``. The fake server's
in-process :class:`~tests.support.fake_mcp_server.Ledger` is the acceptance evidence: every count
asserted below is an exact integer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.types import Tool
from nervos_core.application.job_execution import FailureOutcome
from nervos_core.application.model_completion import (
    MODEL_RATE_LIMITED,
    AssistantTurn,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolResultTurn,
)
from nervos_core.application.retry_policy import PRODUCTION_RETRY_POLICY
from nervos_core.application.tool_invocations import (
    InvocationRequest,
    InvocationStatus,
    RecordOutcomeKind,
    StartOutcomeKind,
)
from nervos_core.application.tool_permissions import PermissionDecision
from nervos_core.domain.jobs import RetryDisposition
from nervos_core.domain.runs import RunStatus
from nervos_core.domain.tools import RiskHints
from nervos_core.infrastructure.database.jobs import SqlAlchemyJobExecutionPersistence
from nervos_core.infrastructure.database.tool_invocations import (
    SqlAlchemyToolInvocationPersistence,
)
from sqlalchemy import Engine, text

from ..support.fake_mcp_server import (
    READ_DOCUMENT,
    WRITE_DOCUMENT,
    FakeMcpServer,
    build_server,
    serve_streamable_http,
)
from ..support.loop_harness import (
    NOW,
    RecordingSleeper,
    ScriptedCompletion,
    add_connection,
    agent_instance_id,
    build_rig,
    claim_run,
    definition_fingerprint,
    descriptor_named,
    discover_connection,
    event_types,
    final_turn,
    grant,
    invocation_rows,
    job_row,
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


class DriftingFakeServer(FakeMcpServer):
    """The same two tools, but able to advertise a materially different ``write_document``.

    Flipping :attr:`drifted` changes the tool's description, which is part of the fingerprint the
    durable definition carries. Rediscovery then updates that definition in place while the grant
    still reviews the old material -- the exact drift D2 must refuse.
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


class DriftingCompletion:
    """Drifts the remote catalog before requesting the tool whose grant it invalidates."""

    def __init__(
        self,
        *,
        engine: Engine,
        connection_id: int,
        endpoint: str,
        server: DriftingFakeServer,
        name: str,
    ) -> None:
        self._engine = engine
        self._connection_id = connection_id
        self._endpoint = endpoint
        self._server = server
        self._name = name
        self.drifted = False
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self.drifted:
            self.drifted = True
            self._server.drifted = True
            await discover_connection(
                self._engine,
                connection_id=self._connection_id,
                config=operator_config(self._endpoint),
                policy=loopback_policy(self._endpoint),
            )
            return tool_turn(tool_call("call-1", self._name, {"path": "a.txt", "content": "one"}))
        return final_turn("drifted")


@pytest.mark.anyio
async def test_read_document_reaches_the_fake_server_through_the_real_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            run_id = submit_run(engine, instance_id=instance_id)
            _, claim = claim_run(engine, run_id)
            run = load_run(engine, run_id)
            events_before = event_types(engine, run_id)
            completion = ScriptedCompletion(
                tool_turn(tool_call("call-1", read.model_name, {"path": "a.txt"})),
                final_turn("the document says document:a.txt"),
            )

            outcome = await rig.loop.run(completion, run, claim, 0)

            # D6 publishes the tool facts a remote call produces, and this is the first place an
            # external source reaches the public timeline. The three events appear in order, all
            # pointing at the durable invocation, and no Stage C event was disturbed.
            events_after = event_types(engine, run_id)
            assert events_before <= events_after
            added = [event for event in events_after if event not in events_before]
            assert sorted(added) == ["tool.requested", "tool.started", "tool.succeeded"]

            assert succeed_run(engine, claim, outcome) is True

            # Exactly one durable invocation, naming the definition discovery wrote.
            rows = invocation_rows(engine)
            assert len(rows) == 1
            assert rows[0]["status"] == InvocationStatus.SUCCEEDED.value
            assert rows[0]["tool_definition_id"] == read.tool_definition_id
            assert rows[0]["source_id"] == connection_id
            assert server.ledger.counts[READ_DOCUMENT] == 1

            # The observation reached the model as a provider-neutral ToolResultTurn.
            assert len(completion.requests) == 2
            turns = completion.requests[1].turns
            assert isinstance(turns[0], AssistantTurn)
            observation = turns[1]
            assert isinstance(observation, ToolResultTurn)
            assert observation.call_id == "call-1"
            assert observation.text == "document:a.txt"
            assert observation.is_error is False
            # No raw SDK object leaked: only the normalized JSON ToolResult reached the loop.
            assert observation.structured == {"result": "document:a.txt"}
            assert type(observation) is ToolResultTurn

            # D6's audit surface carries the fact without the material: the tool event names the
            # invocation and a static message, and neither the remote text nor the argument value
            # is anywhere on it. Asserted here, on the external-source path, because that is where
            # a raw server string would be most tempting to record.
            with engine.connect() as connection:
                audit_text = " ".join(
                    str(value)
                    for row in connection.execute(
                        text(
                            "SELECT event_type, code, message, tool_invocation_id FROM run_events"
                            " WHERE run_id = :r AND event_type LIKE 'tool.%'"
                        ),
                        {"r": run_id},
                    ).all()
                    for value in row
                )
            assert "document:a.txt" not in audit_text
            assert "a.txt" not in audit_text

            assert load_run(engine, run_id).status is RunStatus.SUCCEEDED
        finally:
            await rig.gateway.close_all()


@pytest.mark.anyio
async def test_write_document_records_exactly_one_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate_database(tmp_path, monkeypatch)
    instance_id = agent_instance_id(engine)
    server = build_server()
    async with serve_streamable_http(server) as endpoint:
        config = operator_config(endpoint)
        policy = loopback_policy(endpoint)
        _connection_id, descriptors = await add_connection(
            engine, endpoint=endpoint, config=config, policy=policy
        )
        write = descriptor_named(descriptors, WRITE_DOCUMENT)
        grant(engine, tool_definition_id=write.tool_definition_id, instance_id=instance_id)

        rig = build_rig(engine, config=config, policy=policy)
        try:
            run_id = submit_run(engine, instance_id=instance_id)
            _, claim = claim_run(engine, run_id)
            run = load_run(engine, run_id)
            completion = ScriptedCompletion(
                tool_turn(
                    tool_call("call-1", write.model_name, {"path": "a.txt", "content": "one"})
                ),
                final_turn("wrote it"),
            )

            outcome = await rig.loop.run(completion, run, claim, 0)
            assert succeed_run(engine, claim, outcome) is True

            assert server.ledger.counts[WRITE_DOCUMENT] == 1
            assert server.ledger.entries == ["write_document:a.txt=one"]
            rows = invocation_rows(engine)
            assert len(rows) == 1
            assert rows[0]["status"] == InvocationStatus.SUCCEEDED.value
            assert rows[0]["tool_definition_id"] == write.tool_definition_id
            observation = completion.requests[1].turns[1]
            assert isinstance(observation, ToolResultTurn)
            assert observation.text == "wrote:a.txt"
        finally:
            await rig.gateway.close_all()


@pytest.mark.anyio
async def test_a_rate_limit_after_a_write_never_redispatches_the_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrate_database(tmp_path, monkeypatch)
    instance_id = agent_instance_id(engine)
    server = build_server()
    async with serve_streamable_http(server) as endpoint:
        config = operator_config(endpoint)
        policy = loopback_policy(endpoint)
        _connection_id, descriptors = await add_connection(
            engine, endpoint=endpoint, config=config, policy=policy
        )
        write = descriptor_named(descriptors, WRITE_DOCUMENT)
        grant(engine, tool_definition_id=write.tool_definition_id, instance_id=instance_id)

        sleeper = RecordingSleeper()
        rig = build_rig(engine, config=config, policy=policy, sleep=sleeper)
        try:
            run_id = submit_run(engine, instance_id=instance_id)
            _, claim = claim_run(engine, run_id)
            run = load_run(engine, run_id)
            completion = ScriptedCompletion(
                tool_turn(
                    tool_call("call-1", write.model_name, {"path": "a.txt", "content": "one"})
                ),
                beyond=ModelProviderError(MODEL_RATE_LIMITED, usage=ModelUsage()),
            )

            with pytest.raises(ModelProviderError) as raised:
                await rig.loop.run(completion, run, claim, 0)

            assert raised.value.code == MODEL_RATE_LIMITED
            # The Layer-1 ladder ran to exhaustion: three retries at 1s/2s/4s, then a static raise.
            assert sleeper.delays == [1.0, 2.0, 4.0]
            assert len(completion.requests) == 5
            # The write happened exactly once and was never re-dispatched by the retry ladder.
            assert server.ledger.counts[WRITE_DOCUMENT] == 1
            rows = invocation_rows(engine)
            assert len(rows) == 1
            assert rows[0]["status"] == InvocationStatus.SUCCEEDED.value

            # A dispatched tool call makes the whole Attempt unreplayable, so a rate limit that
            # would otherwise be SAFE_TO_RETRY is settled terminally instead.
            outcome = SqlAlchemyJobExecutionPersistence(engine).record_failure(
                claim,
                error_code=MODEL_RATE_LIMITED,
                error_message="The model provider is temporarily rate limited.",
                retry_disposition=RetryDisposition.SAFE_TO_RETRY,
                usage=ModelUsage(),
                elapsed_ms=0,
                anchor_at=NOW,
                retry_policy=PRODUCTION_RETRY_POLICY,
                now=NOW,
            )
            assert outcome is FailureOutcome.TERMINAL_FAILED

            run_state = load_run(engine, run_id)
            assert run_state.status is RunStatus.FAILED
            assert run_state.error_code == MODEL_RATE_LIMITED
            assert job_row(engine, run_id)["attempt_count"] == 1

            # The source's own annotations (idempotent=False, destructive=True) had no effect: the
            # no-replay outcome is exactly the annotation-blind one.
            assert write.risk_hints == RiskHints(
                read_only=False, destructive=True, idempotent=False, open_world=False
            )
        finally:
            await rig.gateway.close_all()


@pytest.mark.anyio
async def test_write_document_annotations_never_authorize_a_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``destructive``/non-idempotent hint neither denies a call nor licenses re-running it."""
    engine = migrate_database(tmp_path, monkeypatch)
    instance_id = agent_instance_id(engine)
    server = build_server()
    async with serve_streamable_http(server) as endpoint:
        config = operator_config(endpoint)
        policy = loopback_policy(endpoint)
        _connection_id, descriptors = await add_connection(
            engine, endpoint=endpoint, config=config, policy=policy
        )
        write = descriptor_named(descriptors, WRITE_DOCUMENT)
        grant(engine, tool_definition_id=write.tool_definition_id, instance_id=instance_id)

        # The durable descriptor records exactly the adversarial claims the fake server made.
        assert write.risk_hints == RiskHints(
            read_only=False, destructive=True, idempotent=False, open_world=False
        )

        run_id = submit_run(engine, instance_id=instance_id)
        _, claim = claim_run(engine, run_id)
        # A destructive hint does not deny an otherwise-authorized call.
        allowed, _reason = permission(
            engine, run_id=run_id, tool_definition_id=write.tool_definition_id
        )
        assert allowed is True

        # A non-idempotent hint does not license replaying a dispatched call either.
        invocations = SqlAlchemyToolInvocationPersistence(engine)
        recorded = invocations.record_requested(
            claim=claim,
            request=InvocationRequest(
                run_id=claim.run_id,
                job_id=claim.job_id,
                attempt_id=claim.attempt_id,
                tool_sequence=1,
                tool_definition_id=write.tool_definition_id,
                source_kind=write.source_kind,
                source_id=write.source_id,
                upstream_name=write.upstream_name,
                model_name=write.model_name,
                definition_fingerprint=write.fingerprint,
                provider_call_id="call-1",
                permission_decision=PermissionDecision(allowed=True),
                arguments={"path": "a.txt", "content": "one"},
            ),
            now=NOW,
        )
        assert recorded.kind is RecordOutcomeKind.REQUESTED
        assert recorded.invocation_id is not None
        assert (
            invocations.mark_started(
                claim=claim, invocation_id=recorded.invocation_id, now=NOW
            ).kind
            is StartOutcomeKind.STARTED
        )
        outcome = SqlAlchemyJobExecutionPersistence(engine).record_failure(
            claim,
            error_code=MODEL_RATE_LIMITED,
            error_message="The model provider is temporarily rate limited.",
            retry_disposition=RetryDisposition.SAFE_TO_RETRY,
            usage=ModelUsage(),
            elapsed_ms=0,
            anchor_at=NOW,
            retry_policy=PRODUCTION_RETRY_POLICY,
            now=NOW,
        )
        assert outcome is FailureOutcome.TERMINAL_FAILED
        assert server.ledger.counts.get(WRITE_DOCUMENT, 0) == 0


@pytest.mark.anyio
async def test_rediscovery_drift_denies_the_next_live_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fingerprint the user did not review suspends authority on the very next call."""
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
        grant(engine, tool_definition_id=write.tool_definition_id, instance_id=instance_id)

        rig = build_rig(engine, config=config, policy=policy)
        try:
            run_id = submit_run(engine, instance_id=instance_id)
            _, claim = claim_run(engine, run_id)
            run = load_run(engine, run_id)
            completion = DriftingCompletion(
                engine=engine,
                connection_id=connection_id,
                endpoint=endpoint,
                server=server,
                name=write.model_name,
            )

            outcome = await rig.loop.run(completion, run, claim, 0)

            # The rediscovery really did change the durable material the grant reviewed.
            assert definition_fingerprint(engine, write.tool_definition_id) != write.fingerprint
            rows = invocation_rows(engine)
            assert len(rows) == 1
            assert rows[0]["status"] == InvocationStatus.DENIED.value
            assert rows[0]["permission_decision"] == "denied_definition_changed"
            # D2 denied the call before a socket existed, so the server saw nothing.
            assert server.ledger.counts.get(WRITE_DOCUMENT, 0) == 0
            assert outcome.output_text == "drifted"
        finally:
            await rig.gateway.close_all()
