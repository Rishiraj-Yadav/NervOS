"""Execute the generated applications with real immutable SDK port results."""
# pyright: basic

import runpy
from pathlib import Path

import pytest
from nervos_sdk import AgentContext, ModelResult, ToolResult, WorkflowSnapshot

ROOT = Path(__file__).resolve().parents[3]


def _agent(key, name):
    source = runpy.run_path(str(ROOT / "scripts/build_autonomous_workflow_demos.py"))[key]
    namespace = {}
    exec(compile(source, "demo-agent", "exec"), namespace)
    return namespace[name]()


class Tools:
    def __init__(self):
        self.calls = []

    async def invoke(self, request):
        self.calls.append(request.name)
        if request.name == "research.search":
            return ToolResult(
                content={
                    "text": "SQLite stores data locally. Source: https://sqlite.org/about.html",
                    "structured": {},
                }
            )
        if request.name == "gmail.messages":
            return ToolResult(content={"structured": {"messages": [{"id": "abc"}]}})
        return ToolResult(
            content={
                "structured": {"id": "abc", "subject": "Invoice", "body_text": "Please review."}
            }
        )


class Model:
    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResult(output_text="Evidence-based brief citing SQLite.")


@pytest.mark.anyio
async def test_research_retains_tool_evidence_and_uses_model_after_restart():
    tools, model, state = Tools(), Model(), {}
    for step in range(1, 5):
        # New application instance at each boundary, like an exited package host.
        result = await _agent("RESEARCH_AGENT", "ResearchAgent").run(
            AgentContext(
                run_id=str(step),
                agent_instance_id="1",
                input={"text": "Local databases"},
                tools=tools,
                model=model,
                workflow=WorkflowSnapshot(
                    step_number=step, checkpoint_revision=step - 1, state=state
                ),
            )
        )
        state = result.workflow.state
    assert tools.calls == ["research.search"] * 3
    assert len(model.requests) == 1
    assert "sqlite.org" in model.requests[0].messages[1].content
    assert state["brief"] == "Evidence-based brief citing SQLite."
    assert result.workflow.directive.kind == "complete"


@pytest.mark.anyio
async def test_mail_consumes_immutable_bridge_envelope_and_never_sends():
    tools = Tools()
    result = await _agent("MAIL_AGENT", "MailTriageAgent").run(
        AgentContext(
            run_id="1",
            agent_instance_id="1",
            tools=tools,
            configuration={"read_tool_definition_id": 42},
            workflow=WorkflowSnapshot(step_number=1, checkpoint_revision=0, state={}),
        )
    )
    assert result.workflow.state["classified"][0]["category"] == "billing"
    assert result.workflow.directive.decision.tool_definition_id == 42
    assert result.workflow.directive.decision.upstream_name == "gmail.users.messages.get"
    resumed = await _agent("MAIL_AGENT", "MailTriageAgent").run(
        AgentContext(
            run_id="2",
            agent_instance_id="1",
            tools=tools,
            workflow=WorkflowSnapshot(
                step_number=2, checkpoint_revision=1, state=result.workflow.state
            ),
        )
    )
    assert resumed.workflow.directive.kind == "complete"
    assert tools.calls == ["gmail.messages", "gmail.message", "gmail.message"]
