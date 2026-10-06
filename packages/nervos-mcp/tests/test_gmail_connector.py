"""The Gmail read-only connector against a fake provider (W5a).

The properties under test are the ones an owner relies on when they connect a mailbox:

* Connecting a Gmail account grants **reading**, and nothing else. There is no code path to
  send, delete, or modify, and a write operation is refused by name rather than ignored.
* The token is the broker's to hand out. The connector never receives one it did not ask for
  through the broker, and the connection must already carry the exact read-only scope.
* Every read is bounded, so a legitimate grant cannot be turned into an unbounded read of the
  Worker's memory.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any, cast

import httpx2
import pytest
from nervos_core.application.account_actions import AccountActionBroker
from nervos_core.application.account_connections import ConnectionUnavailableError
from nervos_core.application.tool_registry import ToolExecutionFailure, ToolResult
from nervos_core.domain.tools import RiskHints, ToolDescriptor, ToolSourceKind
from nervos_mcp.connectors.gmail import (
    GET_MESSAGE,
    GMAIL_API_ORIGIN,
    GMAIL_READONLY_SCOPE,
    LIST_MESSAGES,
    MAX_BODY_CHARS,
    MAX_MESSAGES_PER_PAGE,
    MAX_QUERY_CHARS,
    MAX_RESPONSE_BYTES,
    READ_ONLY_OPERATIONS,
    GmailReadConnector,
    GmailTransportError,
    HttpGmailTransport,
)
from nervos_mcp.errors import McpErrorCode
from nervos_mcp.policy.egress import StrictEgressPolicy

# pyright: basic


class RecordingBroker:
    """Stands in for the real broker and records what the connector asked it for.

    The broker's own behaviour -- live scope checking, owner re-authorization across the
    await, refresh, and refusing credential-shaped output -- is already proved against a
    real database in ``test_stage_h_account_dispatch.py``. Re-testing it here would only
    assert a fake. What this file must prove is that the *connector* asks correctly.
    """

    def __init__(self, *, scopes: tuple[str, ...] = (GMAIL_READONLY_SCOPE,)) -> None:
        self.scopes = scopes
        self.required: list[str] = []
        self.connection_ids: list[int] = []

    async def dispatch(
        self,
        *,
        run: object,
        claim: object,
        descriptor: object,
        connection_id: int,
        required_scope: str,
        execute: Any,
    ) -> ToolResult:
        self.required.append(required_scope)
        self.connection_ids.append(connection_id)
        if required_scope not in self.scopes:
            raise ConnectionUnavailableError
        return await execute("test-access-token")


class FakeTransport:
    """Records every URL and returns a canned Gmail response. No socket is opened."""

    def __init__(self, body: object, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.urls: list[str] = []
        self.tokens: list[str] = []

    async def get_json(self, url: str, *, access_token: str) -> tuple[int, object]:
        self.urls.append(url)
        self.tokens.append(access_token)
        return self.status, self.body


class LoopbackPolicy:
    """A policy that answers for exactly one origin, refusing everything else."""

    def __init__(self, allowed: str = GMAIL_API_ORIGIN) -> None:
        self.allowed = allowed

    def validate(self, endpoint: str) -> object:
        if endpoint != self.allowed:
            raise _refused()
        return object()


def _refused() -> Exception:
    from nervos_mcp.errors import McpConfigurationError

    return McpConfigurationError(McpErrorCode.ORIGIN_REFUSED)


def _descriptor(upstream: str) -> ToolDescriptor:
    return ToolDescriptor(
        tool_definition_id=1,
        upstream_name=upstream,
        model_name="connector",
        source_kind=ToolSourceKind.BUILTIN,
        source_id=1,
        display_name=upstream,
        description="read",
        input_schema={"type": "object", "properties": {}},
        output_schema=None,
        risk_hints=RiskHints(),
        fingerprint="1" * 64,
    )


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _pair(
    transport: FakeTransport,
    *,
    scopes: tuple[str, ...] = (GMAIL_READONLY_SCOPE,),
    policy: object | None = None,
) -> tuple[GmailReadConnector, RecordingBroker]:
    broker = RecordingBroker(scopes=scopes)
    connector = GmailReadConnector(
        broker=cast("AccountActionBroker", broker),
        transport=cast("Any", transport),
        egress=cast("Any", policy or LoopbackPolicy()),
        connection_id=1,
    )
    return connector, broker


def _connector(transport: FakeTransport, **kwargs: Any) -> GmailReadConnector:
    return _pair(transport, **kwargs)[0]


async def _invoke(
    connector: GmailReadConnector, upstream: str, arguments: Mapping[str, Any]
) -> ToolResult:
    return await connector.invoke(
        run=cast("Any", None),
        claim=cast("Any", None),
        descriptor=_descriptor(upstream),
        arguments=cast("Mapping[str, Any]", arguments),
    )


# ---------------------------------------------------------------------------------------
# The scope is the whole grant
# ---------------------------------------------------------------------------------------


def test_the_connector_asks_for_exactly_one_read_only_scope() -> None:
    # gmail.send, gmail.compose and gmail.modify are deliberately absent: "the account is
    # connected" and "the account can be changed" must stay different permissions.
    assert GMAIL_READONLY_SCOPE == "https://www.googleapis.com/auth/gmail.readonly"
    assert "readonly" in GMAIL_READONLY_SCOPE


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", ["success", "redirect", "oversized", "invalid", "destination"])
async def test_http_transport_is_fixed_origin_bounded_and_redacts_errors(monkeypatch, scenario):
    factory = httpx2.AsyncClient
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer synthetic-test-token"
        if scenario == "redirect":
            return httpx2.Response(302, headers={"Location": "https://evil.invalid"})
        if scenario == "oversized":
            return httpx2.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
        if scenario == "invalid":
            return httpx2.Response(200, content=b"synthetic-test-token")
        return httpx2.Response(200, json={"messages": []})

    def client(**options):
        assert options["trust_env"] is False and options["follow_redirects"] is False
        return factory(transport=httpx2.MockTransport(respond), **options)

    monkeypatch.setattr(httpx2, "AsyncClient", client)
    url = GMAIL_API_ORIGIN + "/gmail/v1/users/me/messages"
    if scenario == "destination":
        url += "/../settings"
    if scenario in ("oversized", "invalid", "destination"):
        with pytest.raises(GmailTransportError) as error:
            await HttpGmailTransport().get_json(url, access_token="synthetic-test-token")
        assert "synthetic-test-token" not in str(error.value)
    else:
        status, body = await HttpGmailTransport().get_json(url, access_token="synthetic-test-token")
        assert status == (302 if scenario == "redirect" else 200)
        assert body == (None if scenario == "redirect" else {"messages": []})
    assert len(calls) == (0 if scenario == "destination" else 1)


def test_the_operations_exposed_are_exactly_list_search_and_read() -> None:
    assert frozenset({LIST_MESSAGES, GET_MESSAGE}) == READ_ONLY_OPERATIONS
    for operation in READ_ONLY_OPERATIONS:
        assert ".list" in operation or ".get" in operation


@pytest.mark.anyio
async def test_search_is_the_list_operation_with_a_query() -> None:
    # Gmail has one endpoint, so a second operation name would be a second tool identity for
    # one API call. `query` is an optional argument of list instead.
    transport = FakeTransport({"messages": []})
    connector = _connector(transport)
    await _invoke(connector, LIST_MESSAGES, {})
    assert "q=" not in transport.urls[0]
    await _invoke(connector, LIST_MESSAGES, {"query": "is:unread"})
    assert "q=is%3Aunread" in transport.urls[1]


@pytest.mark.anyio
async def test_a_write_operation_is_refused_by_name() -> None:
    transport = FakeTransport({})
    connector = _connector(transport)
    with pytest.raises(ToolExecutionFailure, match="read-only"):
        await _invoke(connector, "gmail.users.messages.send", {})
    assert transport.urls == [], "a refused write must not reach the provider"


@pytest.mark.anyio
async def test_an_unrelated_tool_is_not_claimed_by_the_connector() -> None:
    connector = _connector(FakeTransport({}))
    assert connector.applies(_descriptor(GET_MESSAGE)) is True
    assert connector.applies(_descriptor("filesystem.read")) is False


# ---------------------------------------------------------------------------------------
# Token custody and scope
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_token_comes_from_the_broker_and_never_appears_in_the_result() -> None:
    transport = FakeTransport({"messages": [{"id": "m1", "threadId": "t1"}]})
    connector, broker = _pair(transport)
    result = await _invoke(connector, LIST_MESSAGES, {})
    assert transport.tokens == ["test-access-token"]
    assert broker.required == [GMAIL_READONLY_SCOPE], "the connector must ask for its own scope"
    assert "test-access-token" not in result.text


@pytest.mark.anyio
async def test_a_connection_without_the_read_only_scope_is_refused() -> None:
    transport = FakeTransport({"messages": []})
    connector = _connector(transport, scopes=("https://www.googleapis.com/auth/gmail.modify",))
    with pytest.raises(ToolExecutionFailure):
        await _invoke(connector, LIST_MESSAGES, {})
    assert transport.urls == [], "an under-scoped connection must not reach the provider"


# ---------------------------------------------------------------------------------------
# Egress
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_refused_origin_never_dials_out() -> None:
    transport = FakeTransport({"messages": []})
    connector = _connector(transport, policy=LoopbackPolicy("https://elsewhere.example"))
    with pytest.raises(ToolExecutionFailure):
        await _invoke(connector, LIST_MESSAGES, {})
    assert transport.urls == []


def test_a_policy_that_allows_nothing_refuses_the_gmail_origin() -> None:
    policy = StrictEgressPolicy(frozenset())
    with pytest.raises(Exception, match=r"not permitted"):
        policy.validate(GMAIL_API_ORIGIN)


# ---------------------------------------------------------------------------------------
# Bounded reads
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_listing_is_bounded_by_a_caller_chosen_page_size() -> None:
    transport = FakeTransport({"messages": []})
    await _invoke(_connector(transport), LIST_MESSAGES, {"max_results": 25})
    assert "maxResults=25" in transport.urls[0]


@pytest.mark.anyio
async def test_an_oversized_page_is_refused() -> None:
    with pytest.raises(ToolExecutionFailure):
        await _invoke(_connector(FakeTransport({})), LIST_MESSAGES, {"max_results": 10_000})


@pytest.mark.anyio
async def test_an_oversized_query_is_refused_before_the_dial() -> None:
    transport = FakeTransport({"messages": []})
    with pytest.raises(ToolExecutionFailure):
        await _invoke(_connector(transport), LIST_MESSAGES, {"query": "x" * (MAX_QUERY_CHARS + 1)})
    assert transport.urls == []


@pytest.mark.anyio
async def test_a_query_is_encoded_rather_than_spliced() -> None:
    transport = FakeTransport({"messages": []})
    await _invoke(_connector(transport), LIST_MESSAGES, {"query": "subject:hi&maxResults=999"})
    url = transport.urls[0]
    assert "maxResults=999" not in url
    assert "subject%3Ahi" in url or "subject%3ahi" in url


@pytest.mark.anyio
async def test_a_deeply_nested_message_yields_no_body_rather_than_recursing_forever() -> None:
    node: dict[str, Any] = {"mimeType": "multipart/alternative", "parts": []}
    root = node
    for _ in range(200):
        child: dict[str, Any] = {"mimeType": "multipart/alternative", "parts": []}
        node["parts"] = [child]
        node = child
    transport = FakeTransport({"id": "m1", "threadId": "t1", "payload": root})
    result = await _invoke(_connector(transport), GET_MESSAGE, {"message_id": "m1"})
    # The walk stops at its depth bound and reports no body. That is honest: pretending to
    # have found the body, or raising, would both misdescribe what was read.
    assert cast("Mapping[str, Any]", result.structured)["body_text"] == ""


@pytest.mark.anyio
async def test_a_message_body_is_projected_to_bounded_plain_text() -> None:
    transport = FakeTransport(
        {
            "id": "m1",
            "threadId": "t1",
            "labelIds": ["INBOX"],
            "snippet": "hello",
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [{"name": "Subject", "value": "Budget"}],
                "parts": [
                    {"mimeType": "text/html", "body": {"data": _b64("<b>hi</b>")}},
                    {"mimeType": "text/plain", "body": {"data": _b64("hello there")}},
                ],
            },
        }
    )
    result = await _invoke(_connector(transport), GET_MESSAGE, {"message_id": "m1"})
    structured = cast("Mapping[str, Any]", result.structured)
    assert structured["subject"] == "Budget"
    assert structured["body_text"] == "hello there"
    assert isinstance(structured["body_text"], str)
    assert len(cast("str", structured["body_text"])) <= MAX_BODY_CHARS


@pytest.mark.anyio
async def test_a_message_id_cannot_escape_its_path_segment() -> None:
    transport = FakeTransport({"id": "m1", "threadId": "t1"})
    with pytest.raises(ToolExecutionFailure):
        await _invoke(
            _connector(transport), GET_MESSAGE, {"message_id": "../../v1/users/other/messages"}
        )
    assert transport.urls == []


@pytest.mark.anyio
async def test_a_caller_cannot_aim_the_read_at_another_mailbox() -> None:
    # There is no user-id argument at all: the connector reads the connected account, so a
    # package cannot request someone else's mailbox by adding a field.
    transport = FakeTransport({"messages": []})
    await _invoke(_connector(transport), LIST_MESSAGES, {"user_id": "someone@else.example"})
    assert "/users/me/" in transport.urls[0]
    assert "someone@else.example" not in transport.urls[0]


# ---------------------------------------------------------------------------------------
# Malformed provider output
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_oversized_provider_page_is_refused() -> None:
    oversized = MAX_MESSAGES_PER_PAGE + 1
    page = {"messages": [{"id": f"m{i}", "threadId": "t"} for i in range(oversized)]}
    with pytest.raises(ToolExecutionFailure):
        await _invoke(_connector(FakeTransport(page)), LIST_MESSAGES, {})


@pytest.mark.anyio
async def test_a_malformed_message_id_is_refused_rather_than_coerced() -> None:
    # Repairing this would put invented content in a checkpoint an owner will later read as
    # if it were real mail.
    with pytest.raises(ToolExecutionFailure):
        await _invoke(
            _connector(FakeTransport({"messages": [{"id": "m 1/../x", "threadId": "t"}]})),
            LIST_MESSAGES,
            {},
        )


@pytest.mark.anyio
async def test_a_non_200_status_is_not_projected_as_an_empty_inbox() -> None:
    # Presenting an outage as "no messages" would let a workflow mark mail triaged when it
    # never read anything.
    with pytest.raises(ToolExecutionFailure):
        await _invoke(_connector(FakeTransport({}, status=503)), LIST_MESSAGES, {})
