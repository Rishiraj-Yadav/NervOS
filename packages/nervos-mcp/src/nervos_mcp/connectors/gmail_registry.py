"""Native read definitions; registration grants no account or agent authority."""

from collections.abc import Mapping
from typing import NoReturn

from nervos_core.application.builtin_tools import BuiltinToolSpec, reconcile_builtin_definitions
from nervos_core.application.clock import Clock
from nervos_core.application.tool_registry import (
    ToolDefinitionPersistence,
    ToolExecutionFailure,
    ToolFailureReason,
)
from nervos_core.domain.tools import JsonValue, RiskHints
from nervos_mcp.connectors.gmail import GET_MESSAGE, LIST_MESSAGES

LIST_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "max_results": {"type": "integer"},
    },
    "additionalProperties": False,
}
GET_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {"message_id": {"type": "string"}},
    "required": ["message_id"],
    "additionalProperties": False,
}


def _refuse(arguments: Mapping[str, JsonValue]) -> NoReturn:
    raise ToolExecutionFailure(ToolFailureReason.INTERNAL, "Gmail account binding is unavailable")


def register_gmail_reads(persistence: ToolDefinitionPersistence, clock: Clock) -> None:
    reconcile_builtin_definitions(
        persistence,
        specs=(
            BuiltinToolSpec(
                LIST_MESSAGES,
                "Gmail list/search",
                "Read message references from a connected mailbox",
                LIST_SCHEMA,
                None,
                RiskHints(),
                _refuse,
            ),
            BuiltinToolSpec(
                GET_MESSAGE,
                "Gmail read",
                "Read one message from a connected mailbox",
                GET_SCHEMA,
                None,
                RiskHints(),
                _refuse,
            ),
        ),
        now=clock(),
    )
