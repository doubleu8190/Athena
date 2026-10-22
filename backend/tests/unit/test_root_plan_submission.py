from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.orchestration.root_workflow import RootWorkflow
from domain.orchestration import OrchestrationCommandType


class ToolService:
    async def list_tools(self):
        return [
            SimpleNamespace(
                name="read_local_file",
                description="Read a workspace file",
                parameters={},
                risk_level="low",
                execution_mode="native",
                enabled=True,
                require_approval=False,
            ),
            SimpleNamespace(
                name="disabled_tool",
                description="Disabled tool",
                parameters={},
                risk_level="low",
                execution_mode="native",
                enabled=False,
                require_approval=False,
            ),
        ]


class CommandHandler:
    def __init__(self):
        self.submissions = []

    async def submit_plan(self, command, plan, tasks, *, legal_tools=None):
        self.submissions.append((command, plan, tasks, legal_tools))


class Scheduler:
    def __init__(self):
        self.started = []

    async def start_ready_tasks(self, plan_id):
        self.started.append(plan_id)


@pytest.mark.asyncio
async def test_root_submits_initial_plan_through_atomic_command_handler():
    handler = CommandHandler()
    scheduler = Scheduler()
    legacy_orchestration = SimpleNamespace(create_plan=lambda **_kwargs: None)
    workflow = RootWorkflow(
        SimpleNamespace(
            tool_service=ToolService(),
            orchestration_command_handler=handler,
            orchestration=legacy_orchestration,
            worker_scheduler=scheduler,
            persist_plan=None,
            validate_plan=None,
            event_publisher=None,
            plan_builder=None,
        )
    )

    result = await workflow.materialize_execution_plan(
        {
            "request": {
                "run_id": "run-42",
                "session_id": "session-9",
                "user_message": "Prepare a report",
            },
            "understanding": {"task_spec": {"goal": "Prepare a report"}},
            "plan": {
                "plan_id": "report-plan",
                "goal": "Prepare a report",
                "max_parallelism": 2,
                "tasks": [
                    {
                        "task_id": "gather",
                        "title": "Gather data",
                        "objective": "Collect local source data",
                        "allowed_tools": ["read_local_file", "disabled_tool"],
                    }
                ],
            },
        }
    )

    assert len(handler.submissions) == 1
    command, plan, tasks, legal_tools = handler.submissions[0]
    assert command.command_type is OrchestrationCommandType.SUBMIT_PLAN
    assert command.message_id == "root-submit-plan:run-42"
    assert command.idempotency_key == "submit-plan:run-42"
    assert command.correlation_id == command.scope_key == "run-42"
    assert command.plan_id == plan.plan_id == "report-plan:run-42"
    assert command.plan_version == 1
    assert plan.session_id == "session-9"
    assert plan.run_id == "run-42"
    assert tasks[0].task_id == "gather:run-42"
    assert tasks[0].allowed_tools == ("read_local_file",)
    assert legal_tools == {"read_local_file"}
    assert scheduler.started == []
    assert result["plan"]["plan_id"] == plan.plan_id
