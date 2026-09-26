"""Agent entrypoint protocol for the NervOS SDK."""

from __future__ import annotations

from typing import Protocol

from nervos_sdk.types import AgentContext, AgentResult


class AgentEntrypoint(Protocol):
    """Protocol implemented by async NervOS agent entrypoints.

    One call to :meth:`run` is one Worker-owned Attempt execution step. An implementation claims no
    Jobs, creates no Attempts, decides no retries, persists no state, receives no credentials, and
    schedules nothing: the Worker owns all of that and supplies the context.
    """

    async def run(self, context: AgentContext) -> AgentResult: ...
