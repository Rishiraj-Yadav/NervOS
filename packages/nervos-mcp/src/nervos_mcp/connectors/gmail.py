"""The Gmail read-first connector (ADR 0039, W5).

This is *controlled external access*. The connector decides what an operator has authorized a
connected account to expose; an agent package supplies only the application logic that calls
it. Installing a package, or connecting an account, therefore grants no agent anything on
its own -- a package must still be granted the tool, and the owner must still connect the
account with the exact scope this connector declares.

**Read-only, and only by construction.** Every operation here maps to a Gmail ``users.messages``
read: ``list``, ``search`` and ``get``. There is no send, no delete, no label mutation, no
draft creation, and no ``users.settings`` or ``users.drafts`` path at all. That is not a
convention a future change is expected to respect -- it is the absence of code. A connector
that could also write would make "the account is connected" and "the account can be changed"
the same permission, and the owner could no longer grant the first without the second.

Everything else is inherited rather than re-invented: the broker holds the token, checks the
scope, re-authorizes the owner across the await, and refuses credential-shaped output. This
module owns the Gmail request shape, the bounds, and the refusal of anything else.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import quote, urlsplit

import httpx2
from nervos_core.application.account_actions import AccountActionBroker
from nervos_core.application.account_connections import ConnectionUnavailableError
from nervos_core.application.tool_invocations import ClaimHandle
from nervos_core.application.tool_registry import (
    ToolExecutionFailure,
    ToolFailureReason,
    ToolResult,
)
from nervos_core.domain.runs import Run
from nervos_core.domain.tools import JsonValue, ToolDescriptor
from nervos_mcp.errors import McpConfigurationError, McpProtocolError
from nervos_mcp.policy.egress import EgressPolicy

#: The one scope this connector will ever ask for. ``gmail.readonly`` covers list, search,
#: and read. Sending is ``gmail.send``; drafts are ``gmail.compose``; changing anything is
#: ``gmail.modify``. None of those are requested, so none of them are ever consented to.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

#: The only origin this connector will dial, and only after the operator's egress allowlist
#: has independently agreed. Both must say yes: the constant stops a configuration mistake,
#: the policy stops a misconfiguration.
GMAIL_API_ORIGIN = "https://gmail.googleapis.com"

#: Bounds. A mail read that could return an unbounded body, or an unbounded page, would turn
#: a read-only connector into a way to exhaust the Worker's memory through a legitimate grant.
MAX_MESSAGES_PER_PAGE = 50
DEFAULT_MESSAGES_PER_PAGE = 10
MAX_QUERY_CHARS = 512
MAX_SUBJECT_CHARS = 512
MAX_BODY_CHARS = 16 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
REQUEST_TIMEOUT_SECONDS = 20.0

#: The characters Gmail uses in a message or thread id. Anything else means the response was
#: not what this connector asked for, and is refused rather than passed into a URL.
_ID_ALPHABET = frozenset("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_")

# Gmail has no separate search endpoint: search *is* the list endpoint with a `q` parameter.
# Inventing a second operation name that maps to the same upstream would have created two
# tool identities for one API call, so there is deliberately only one list/search operation
# and `query` is an optional argument of it.
LIST_MESSAGES = "gmail.users.messages.list"
GET_MESSAGE = "gmail.users.messages.get"
READ_ONLY_OPERATIONS = frozenset({LIST_MESSAGES, GET_MESSAGE})


class GmailTransportError(RuntimeError):
    """Gmail refused, misbehaved, or returned something this connector will not pass on."""


class GmailTransport(Protocol):
    """The narrow HTTP seam, so tests can supply a fake provider with no socket."""

    async def get_json(self, url: str, *, access_token: str) -> tuple[int, object]: ...


class HttpGmailTransport:
    """Bounded fixed-origin GET; redirects and ambient proxy credentials are refused."""

    async def get_json(self, url: str, *, access_token: str) -> tuple[int, object]:
        parsed = urlsplit(url)
        base = "/gmail/v1/users/me/messages"
        path = parsed.path
        identifier = path[len(base) + 1 :] if path.startswith(base + "/") else ""
        if (
            parsed.scheme != "https"
            or parsed.netloc != "gmail.googleapis.com"
            or parsed.fragment
            or (
                path != base
                and (
                    not identifier
                    or len(identifier) > 128
                    or any(char not in _ID_ALPHABET for char in identifier)
                )
            )
        ):
            raise GmailTransportError("Gmail destination refused")
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                async with httpx2.AsyncClient(
                    timeout=15, follow_redirects=False, trust_env=False
                ) as client:
                    async with client.stream(
                        "GET", url, headers={"Authorization": "Bearer " + access_token}
                    ) as response:
                        if response.status_code != 200:
                            return response.status_code, None
                        payload = bytearray()
                        async for chunk in response.aiter_bytes():
                            payload.extend(chunk)
                            if len(payload) > MAX_RESPONSE_BYTES:
                                raise GmailTransportError("Gmail response exceeds the byte limit")
                        return response.status_code, json.loads(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise GmailTransportError("Gmail request unavailable") from None


@dataclass(frozen=True, slots=True)
class GmailMessageRef:
    """One message id and its label summary, as returned by a list or search page."""

    id: str
    thread_id: str
    label_ids: tuple[str, ...]


class GmailReadConnector:
    """Expose list/search/read over one owner-connected Gmail account."""

    def __init__(
        self,
        *,
        broker: AccountActionBroker,
        transport: GmailTransport,
        egress: EgressPolicy,
        connection_id: int,
    ) -> None:
        self._broker = broker
        self._transport = transport
        self._egress = egress
        self._connection_id = connection_id

    def applies(self, descriptor: ToolDescriptor) -> bool:
        return descriptor.upstream_name in READ_ONLY_OPERATIONS

    async def invoke(
        self,
        *,
        run: Run,
        claim: ClaimHandle,
        descriptor: ToolDescriptor,
        arguments: Mapping[str, JsonValue],
    ) -> ToolResult:
        if descriptor.upstream_name not in READ_ONLY_OPERATIONS:
            # Not merely unknown: a *write* operation reaching this connector is refused by
            # name, so the failure is legible in the audit trail rather than a generic 4xx.
            raise ToolExecutionFailure(
                ToolFailureReason.INTERNAL,
                "Gmail connector exposes read-only operations only",
            )
        try:
            url = self._plan(descriptor.upstream_name, arguments)
        except ValueError as error:
            raise ToolExecutionFailure(ToolFailureReason.INTERNAL, str(error)) from None

        async def execute(token: str) -> ToolResult:
            try:
                # Re-validated here, immediately before the dial, not once at construction:
                # an operator who narrows the allowlist must take effect on the next call.
                self._egress.validate(GMAIL_API_ORIGIN)
                status, body = await self._transport.get_json(url, access_token=token)
            except McpConfigurationError:
                raise
            except Exception as error:
                # Preserve ambiguity: a transport failure is not evidence that no read
                # happened, and this is a read, so there is no effect to reconcile. Still,
                # it is reported as unavailable rather than as an empty inbox.
                raise GmailTransportError("Gmail request failed") from error
            if status != 200:
                raise GmailTransportError(f"Gmail returned status {status}")
            return self._project(descriptor.upstream_name, body)

        try:
            return await self._broker.dispatch(
                run=run,
                claim=claim,
                descriptor=descriptor,
                connection_id=self._connection_id,
                required_scope=GMAIL_READONLY_SCOPE,
                execute=execute,
            )
        except GmailTransportError as error:
            # A read has no external effect to reconcile, so a bad or failed response is a
            # classified tool failure rather than an ambiguous outcome. Letting it escape
            # would crash the Run with an unclassified exception.
            raise ToolExecutionFailure(ToolFailureReason.INTERNAL, str(error)) from None
        except (McpConfigurationError, McpProtocolError):
            # The egress policy said no. Reported as a classified failure so the audit trail
            # records a refused dial rather than an unclassified crash.
            raise ToolExecutionFailure(
                ToolFailureReason.INTERNAL, ConnectionUnavailableError.MESSAGE
            ) from None
        except ConnectionUnavailableError:
            raise ToolExecutionFailure(
                ToolFailureReason.INTERNAL, ConnectionUnavailableError.MESSAGE
            ) from None

    # -- request planning ---------------------------------------------------------------

    def _plan(self, operation: str, arguments: Mapping[str, JsonValue]) -> str:
        """Resolve one read into a fully-formed Gmail URL, refusing anything unexpected.

        The path is built from a fixed template and bounded values, never from caller text
        spliced into a URL. A user id, for instance, is not accepted at all: the connector
        reads the *connected* account, so there is no argument by which a package could aim
        the read at a different mailbox.
        """
        if operation == GET_MESSAGE:
            message_id = _argument_identifier(arguments.get("message_id"), "message_id")
            return f"{GMAIL_API_ORIGIN}/gmail/v1/users/me/messages/{message_id}"
        page_size = _page_size(arguments.get("max_results"))
        base = f"{GMAIL_API_ORIGIN}/gmail/v1/users/me/messages?maxResults={page_size}"
        query = arguments.get("query")
        if query is None:
            return base
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string when supplied")
        if len(query) > MAX_QUERY_CHARS:
            raise ValueError(f"query exceeds {MAX_QUERY_CHARS} characters")
        if any(ord(ch) < 32 for ch in query):
            raise ValueError("query contains control characters")
        return f"{base}&q={_quote(query)}"

    # -- response projection -----------------------------------------------------------

    def _project(self, operation: str, body: object) -> ToolResult:
        """Reduce the provider response to a bounded, owner-readable projection.

        Gmail returns base64url message bodies. Those are projected down to a bounded plain
        snippet rather than passed through, so a workflow checkpoint never carries an
        unbounded encoded blob that it could not show an owner or re-read meaningfully.
        """
        if operation == GET_MESSAGE:
            return self._message(body)
        payload = _mapping(body, "Gmail response")
        raw = payload.get("messages", [])
        if not isinstance(raw, list):
            raise GmailTransportError("Gmail returned an unusable message page")
        entries = cast("list[object]", raw)
        if len(entries) > MAX_MESSAGES_PER_PAGE:
            raise GmailTransportError("Gmail returned an unusable message page")
        messages: list[JsonValue] = []
        for item in entries:
            entry = _mapping(item, "message reference")
            reference: dict[str, JsonValue] = {
                "id": _response_identifier(entry.get("id"), "message id"),
                "thread_id": _response_identifier(entry.get("threadId"), "thread id"),
                "label_ids": list(_labels(entry.get("labelIds"))),
            }
            messages.append(reference)
        return ToolResult(
            text=f"Found {len(messages)} message(s).",
            structured={
                "messages": messages,
                "next_page_token": _page_token(payload.get("nextPageToken")),
                "result_size_estimate": _small_int(payload.get("resultSizeEstimate")),
                "provider_bytes": _encoded_estimate(body),
            },
        )

    def _message(self, body: object) -> ToolResult:
        payload = _mapping(body, "Gmail message")
        headers = _headers(payload.get("payload"))
        snippet = payload.get("snippet")
        return ToolResult(
            text=str(payload.get("snippet") or headers.get("Subject", "(no subject)"))[
                :MAX_SUBJECT_CHARS
            ],
            structured={
                "id": _response_identifier(payload.get("id"), "message id"),
                "thread_id": _response_identifier(payload.get("threadId"), "thread id"),
                "label_ids": list(_labels(payload.get("labelIds"))),
                "subject": headers.get("Subject", "")[:MAX_SUBJECT_CHARS],
                "from": headers.get("From", "")[:MAX_SUBJECT_CHARS],
                "to": headers.get("To", "")[:MAX_SUBJECT_CHARS],
                "date": headers.get("Date", "")[:MAX_SUBJECT_CHARS],
                "snippet": (snippet if isinstance(snippet, str) else "")[:MAX_BODY_CHARS],
                "body_text": _body_text(payload.get("payload"))[:MAX_BODY_CHARS],
                "provider_bytes": _encoded_estimate(body),
            },
        )


# ---------------------------------------------------------------------------------------
# Bounded parsing helpers. Each refuses rather than coercing: a malformed provider response
# is a provider problem, and silently repairing it would put invented content in a workflow
# checkpoint that an owner will later read as if it were real mail.
# ---------------------------------------------------------------------------------------


def _response_identifier(value: object, field: str) -> str:
    """An id the *provider* returned. Unusable means the provider misbehaved."""
    return _identifier(value, field, GmailTransportError)


def _argument_identifier(value: object, field: str) -> str:
    """An id the *caller* supplied. Unusable means the call was malformed."""
    return _identifier(value, field, ValueError)


def _identifier(value: object, field: str, error: type[Exception]) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise error(f"{field} is unusable")
    if any(ch not in _ID_ALPHABET for ch in value):
        raise error(f"{field} is unusable")
    return value


def _labels(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(cast("list[object]", value)) > 64:
        raise GmailTransportError("Gmail labels are unusable")
    labels: list[str] = []
    for item in cast("list[object]", value):
        if not isinstance(item, str) or len(item) > 64:
            raise GmailTransportError("Gmail label is unusable")
        labels.append(item)
    return tuple(labels)


def _page_token(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 512:
        raise GmailTransportError("Gmail page token is unusable")
    return value


def _small_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise GmailTransportError("Gmail result size estimate is unusable")
    return value


def _page_size(value: object) -> int:
    if value is None:
        return DEFAULT_MESSAGES_PER_PAGE
    if type(value) is not int or not 1 <= value <= MAX_MESSAGES_PER_PAGE:
        raise ValueError(f"max_results must be 1..{MAX_MESSAGES_PER_PAGE}")
    return value


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise GmailTransportError(f"{field} is not an object")
    return cast("Mapping[str, object]", value)


def _header(name: str) -> str:
    canonical = name.strip().lower()
    return {"subject": "Subject", "from": "From", "to": "To", "date": "Date"}.get(
        canonical, canonical
    )


def _headers(payload: object) -> Mapping[str, str]:
    """First value per header name, from a possibly nested MIME tree."""
    if payload is None:
        return {}
    headers = _mapping(payload, "Gmail payload").get("headers")
    if headers is None:
        return {}
    if not isinstance(headers, list) or len(cast("list[object]", headers)) > 200:
        raise GmailTransportError("Gmail headers are unusable")
    result: dict[str, str] = {}
    for item in cast("list[object]", headers):
        entry = _mapping(item, "header")
        name, value = entry.get("name"), entry.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise GmailTransportError("Gmail header is unusable")
        # Stored under the same canonical casing the lookups use. Gmail returns "Subject";
        # reading it back under any other casing would silently yield "".
        result.setdefault(_header(name), value)
    return result


def _body_text(payload: object, depth: int = 0) -> str:
    """The first plain-text part, base64url-decoded and bounded.

    ``depth`` is bounded because Gmail MIME parts nest; an unbounded walk over a hostile or
    simply very deep message would be a denial of service inside a *read*.
    """
    if depth > 8 or payload is None:
        return ""
    node = _mapping(payload, "Gmail payload part")
    if node.get("mimeType") == "text/plain":
        return _decode(node.get("body"))
    raw_parts = node.get("parts")
    if raw_parts is None:
        return ""
    if not isinstance(raw_parts, list) or len(cast("list[object]", raw_parts)) > 64:
        raise GmailTransportError("Gmail message parts are unusable")
    for child in cast("list[object]", raw_parts):
        if isinstance(child, dict):
            text = _body_text(cast("Mapping[str, object]", child), depth + 1)
            if text:
                return text
    return ""


def _decode(body: object) -> str:
    data = _mapping(body, "Gmail body").get("data")
    if not isinstance(data, str) or len(data) > 4 * MAX_BODY_CHARS:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except Exception:
        return ""
    return raw.decode("utf-8", errors="replace")


def _encoded_estimate(body: object) -> int:
    """Approximate the provider response size, for the audit metadata.

    Measured rather than trusted: a connector that logged "200 OK" while streaming an
    unbounded body would make the audit trail lie about what crossed the boundary.
    """
    try:
        return len(json.dumps(body, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _quote(value: str) -> str:

    return quote(value, safe="", encoding="utf-8")
