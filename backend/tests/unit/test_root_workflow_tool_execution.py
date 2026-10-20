"""Root workflow command routing and approval regression tests."""

from __future__ import annotations

from application.orchestration import RootWorkflow, RootWorkflowDependencies
from application.runs.task_understanding import TaskUnderstandingService
from domain.tools import ToolExecutionResult


class ToolService:
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        return ToolExecutionResult("success", output="REAL_STDOUT")


class Events:
    def __init__(self) -> None:
        self.values = []

    async def publish(self, event):
        self.values.append(event)
        return event


class ApprovalService:
    async def request(self, **kwargs):
        from types import SimpleNamespace

        self.requested = kwargs
        return SimpleNamespace(approval_id="approval-1")


async def make_state(workflow: RootWorkflow, message: str) -> dict:
    state = {
        "request": {
            "session_id": "session-1",
            "run_id": "run-1",
            "user_message": message,
        }
    }
    state.update(await workflow.understand_task(state))
    return state


async def test_root_workflow_executes_shell_and_emits_real_tool_events() -> None:
    tools, events = ToolService(), Events()
    workflow = RootWorkflow(
        RootWorkflowDependencies(
            task_understanding=TaskUnderstandingService(),
            tool_service=tools,
            event_publisher=events,
        )
    )
    state = await make_state(workflow, "执行命令 printf REAL_STDOUT")

    result = await workflow.run_root_execution_loop(state)

    assert len(tools.requests) == 1
    assert tools.requests[0].tool_name == "exec_shell"
    assert tools.requests[0].arguments == {"command": "printf REAL_STDOUT"}
    assert result["response"]["content"] == "REAL_STDOUT"
    assert [event.event_type for event in events.values] == [
        "tool.started",
        "tool.completed",
    ]


async def test_root_workflow_marks_run_failed_when_sandbox_is_unavailable() -> None:
    class UnavailableToolService:
        async def execute(self, _request):
            return ToolExecutionResult("sandbox_unavailable", error="docker socket unavailable")

    events = Events()
    workflow = RootWorkflow(
        RootWorkflowDependencies(
            task_understanding=TaskUnderstandingService(),
            tool_service=UnavailableToolService(),
            event_publisher=events,
        )
    )
    state = await make_state(workflow, "执行命令 printf SHOULD_NOT_RUN_ON_HOST")

    result = await workflow.run_root_execution_loop(state)

    assert result["status"] == "failed"
    assert result["response"]["error"]["code"] == "sandbox_unavailable"
    assert result["response"]["content"] == "docker socket unavailable"


async def test_root_workflow_waits_for_requested_tool_approval() -> None:
    tools, approvals = ToolService(), ApprovalService()
    workflow = RootWorkflow(
        RootWorkflowDependencies(
            task_understanding=TaskUnderstandingService(),
            tool_service=tools,
            approval_service=approvals,
        )
    )
    state = await make_state(workflow, "需要批准后执行命令 printf APPROVED")

    result = await workflow.run_root_execution_loop(state)

    assert result["waiting"] is True
    assert result["response"]["approval_id"] == "approval-1"
    assert approvals.requested["arguments"] == {"command": "printf APPROVED"}
    assert tools.requests == []


async def test_root_workflow_resume_clears_checkpoint_waiting_state() -> None:
    tools, events = ToolService(), Events()
    workflow = RootWorkflow(
        RootWorkflowDependencies(
            task_understanding=TaskUnderstandingService(),
            tool_service=tools,
            event_publisher=events,
        )
    )
    state = await make_state(workflow, "需要批准后执行命令 printf APPROVED")
    state.update(
        {
            "waiting": True,
            "status": "waiting",
            "resume_value": {"decision": "approved", "tool_call_id": "tool:run-1"},
        }
    )

    result = await workflow.run_root_execution_loop(state)

    assert result["waiting"] is False
    assert result["status"] == "completed"
    assert result["response"]["content"] == "REAL_STDOUT"
