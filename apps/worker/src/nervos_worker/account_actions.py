"""Operator-reviewed account/tool bindings; credentials stay in the Worker."""

from collections.abc import Mapping

from nervos_core.application.account_actions import AccountActionBroker
from nervos_core.application.account_connections import ConnectionUnavailableError
from nervos_core.application.tool_invocations import ClaimHandle
from nervos_core.application.tool_registry import (
    ToolExecutionFailure,
    ToolFailureReason,
    ToolResult,
)
from nervos_core.domain.runs import Run
from nervos_core.domain.tools import JsonValue, ToolDescriptor, ToolSourceKind
from nervos_mcp.connectors.gmail import (
    GMAIL_API_ORIGIN,
    GMAIL_READONLY_SCOPE,
    READ_ONLY_OPERATIONS,
    GmailReadConnector,
    HttpGmailTransport,
)
from nervos_mcp.errors import McpConfigurationError, McpProtocolError
from nervos_mcp.executor import normalize_mcp_result
from nervos_mcp.gateway import McpGateway
from nervos_mcp.operator_config import SecretValue
from nervos_mcp.policy.egress import EgressPolicy
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class AccountToolBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    tool_definition_id: int = Field(gt=0)
    mcp_connection_id: int | None = Field(default=None, gt=0)
    account_connection_id: int = Field(gt=0)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_scope: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=2048)


def load_account_bindings(raw: str) -> tuple[AccountToolBinding, ...]:
    if not raw:
        return ()
    try:
        bindings = tuple(TypeAdapter(list[AccountToolBinding]).validate_json(raw))
        if len({item.tool_definition_id for item in bindings}) != len(bindings):
            raise ValueError
        return bindings
    except Exception:
        raise ValueError("Account tool bindings are invalid") from None


class WorkerAccountActionDispatcher:
    def __init__(
        self,
        broker: AccountActionBroker,
        gateway: McpGateway,
        bindings: tuple[AccountToolBinding, ...],
        gmail_egress: EgressPolicy | None = None,
    ) -> None:
        self._broker = broker
        self._gateway = gateway
        self._bindings = {item.tool_definition_id: item for item in bindings}
        self._gmail_egress = gmail_egress

    def applies(self, descriptor: ToolDescriptor) -> bool:
        return descriptor.tool_definition_id in self._bindings

    async def invoke(
        self,
        *,
        run: Run,
        claim: ClaimHandle,
        descriptor: ToolDescriptor,
        arguments: Mapping[str, JsonValue],
    ) -> ToolResult:
        binding = self._bindings[descriptor.tool_definition_id]
        if descriptor.source_kind == ToolSourceKind.BUILTIN:
            if (
                descriptor.upstream_name not in READ_ONLY_OPERATIONS
                or descriptor.fingerprint != binding.fingerprint
                or binding.required_scope != GMAIL_READONLY_SCOPE
                or binding.endpoint != GMAIL_API_ORIGIN
                or binding.mcp_connection_id is not None
                or self._gmail_egress is None
            ):
                raise ToolExecutionFailure(
                    ToolFailureReason.INTERNAL, ConnectionUnavailableError.MESSAGE
                )
            return await GmailReadConnector(
                broker=self._broker,
                transport=HttpGmailTransport(),
                egress=self._gmail_egress,
                connection_id=binding.account_connection_id,
            ).invoke(run=run, claim=claim, descriptor=descriptor, arguments=arguments)
        if (
            descriptor.source_kind != ToolSourceKind.MCP
            or descriptor.source_id != binding.mcp_connection_id
            or descriptor.fingerprint != binding.fingerprint
        ):
            raise ToolExecutionFailure(
                ToolFailureReason.INTERNAL, ConnectionUnavailableError.MESSAGE
            )

        async def execute(token: str) -> ToolResult:
            assert binding.mcp_connection_id is not None
            result = await self._gateway.call_tool_with_credential(
                binding.mcp_connection_id,
                descriptor.upstream_name,
                dict(arguments),
                SecretValue(token),
                expected_endpoint=binding.endpoint,
            )
            return normalize_mcp_result(result, descriptor)

        try:
            return await self._broker.dispatch(
                run=run,
                claim=claim,
                descriptor=descriptor,
                connection_id=binding.account_connection_id,
                required_scope=binding.required_scope,
                execute=execute,
            )
        except (ConnectionUnavailableError, McpConfigurationError, McpProtocolError):
            raise ToolExecutionFailure(
                ToolFailureReason.INTERNAL, ConnectionUnavailableError.MESSAGE
            ) from None
