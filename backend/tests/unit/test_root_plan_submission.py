from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.orchestration.root_workflow import RootWorkflow
from application.orchestration.plan_versions import deferred_version_review_decision
from domain.orchestration import OrchestrationCommandType, ReviewAction, ReviewContext
from tests.fakes.leases import lease_context, lease_grant


class CommandHandler:
    def __init__(self):
        self.submissions = []

    async def submit_plan(self, command, plan, tasks, *, legal_tools=None):
        self.submissions.append((command, plan, tasks, legal_tools))


def _workflow(handler):
    return RootWorkflow(
        SimpleNamespace(
            orchestration_command_handler=handler,
            event_publisher=None,
        )
    )


def _plan_task(task_id="gather", **updates):
    return {
        "task_id": task_id,
        "title": "Gather data",
        "objective": "Collect local source data",
        "allowed_tools": [],
        "input": {"source": "workspace"},
        "expected_output": {"format": "summary"},
        **updates,
    }


def _plan_state(*, run_id="run-42", tasks=None, edges=None, tools=None):
    return {
        "request": {
            "run_id": run_id,
            "session_id": "session-9",
            "user_message": "Prepare a report with evidence.",
        },
        "execution_lease": lease_context(
            lease_grant("root_execution", run_id)
        ),
        "worker_tools": tools or [
            {"name": "read_local_file"},
            {"name": "exec_shell"},
        ],
        "plan_draft": {
            "action": "plan",
            "goal": "Prepare a report",
            "max_parallelism": 2,
            "edges": edges or [],
            "tasks": tasks or [_plan_task()],
        },
    }


@pytest.mark.asyncio
async def test_root_submits_initial_plan_through_atomic_command_handler():
    handler = CommandHandler()
    workflow = _workflow(handler)
    tasks = [
        _plan_task(
            "gather",
            allowed_tools=["read_local_file"],
            input={"source": "local files"},
        ),
        _plan_task(
            "summarize",
            title="Summarize data",
            objective="Summarize the collected source data",
            input={"source_task": "gather"},
            expected_output={"format": "report"},
        ),
    ]
    state = _plan_state(
        tasks=tasks,
        edges=[{"from_task_id": "gather", "to_task_id": "summarize"}],
    )

    result = await workflow.materialize_execution_plan(state)

    assert len(handler.submissions) == 1
    command, plan, submitted_tasks, legal_tools = handler.submissions[0]
    assert command.command_type is OrchestrationCommandType.SUBMIT_PLAN
    assert command.message_id == "root-submit-plan:run-42"
    assert command.idempotency_key == "submit-plan:run-42"
    assert command.correlation_id == command.scope_key == "run-42"
    assert command.plan_id == plan.plan_id == "plan:run-42"
    assert command.plan_version == 1
    assert plan.session_id == "session-9"
    assert plan.run_id == "run-42"
    assert plan.goal == "Prepare a report with evidence."
    assert submitted_tasks[0].task_id == "gather:run-42"
    assert submitted_tasks[0].allowed_tools == ("read_local_file",)
    assert [(edge.from_task_id, edge.to_task_id) for edge in plan.edges] == [
        ("gather:run-42", "summarize:run-42")
    ]
    assert legal_tools == {"read_local_file", "exec_shell"}
    assert result["plan"]["plan_id"] == plan.plan_id


@pytest.mark.asyncio
async def test_root_rejects_a_tool_that_is_not_in_the_system_catalog():
    handler = CommandHandler()
    workflow = _workflow(handler)
    state = _plan_state(
        tasks=[_plan_task(allowed_tools=["disabled_tool"])],
        tools=[{"name": "read_local_file"}],
    )

    with pytest.raises(ValueError, match="root_plan_requests_unavailable_tools"):
        await workflow.materialize_execution_plan(state)

    assert handler.submissions == []


@pytest.mark.asyncio
async def test_user_task_input_does_not_grant_tool_permission():
    handler = CommandHandler()
    workflow = _workflow(handler)
    state = _plan_state(
        tasks=[
            _plan_task(
                "command",
                input={"exec_shell": {"command": "python --version"}},
            )
        ]
    )

    await workflow.materialize_execution_plan(state)

    _, _, tasks, _ = handler.submissions[0]
    assert tasks[0].allowed_tools == ()


@pytest.mark.asyncio
async def test_materialization_splits_explicit_versions_before_atomic_submission():
    handler = CommandHandler()
    workflow = _workflow(handler)
    state = _plan_state(
        run_id="run-versioned",
        tasks=[
            _plan_task("draft", plan_version=1),
            _plan_task(
                "review",
                title="Review",
                objective="Review the draft",
                plan_version=2,
                depends_on=["draft"],
            ),
        ],
    )

    result = await workflow.materialize_execution_plan(state)

    assert [task.task_id for task in handler.submissions[0][2]] == ["draft:run-versioned"]
    assert result["deferred_plan_versions"] == {
        "2": [
            {
                "task_id": "review",
                "title": "Review",
                "objective": "Review the draft",
                "allowed_tools": [],
                "input": {"source": "workspace"},
                "expected_output": {"format": "summary"},
                "depends_on": ["draft"],
            }
        ]
    }


def test_completed_version_review_automatically_submits_deferred_version():
    context = ReviewContext(
        original_goal="Version 1 drafts; Version 2 reviews after Root Review",
        plan_id="plan:run-1",
        plan_version=1,
        round_id="round-1",
        full_dag_summary={},
        round_results=({"task_id": "draft:run-1", "status": "completed"},),
    )
    decision = deferred_version_review_decision(
        context,
        {
            "request": {"run_id": "run-1"},
            "plan": {"task_ids": ["draft:run-1"]},
            "deferred_plan_versions": {
                "2": [
                    {
                        "task_id": "review",
                        "title": "Review",
                        "objective": "Review the draft",
                        "input": {"source": "draft"},
                        "expected_output": {"format": "review"},
                        "allowed_tools": [],
                        "depends_on": ["draft"],
                    }
                ]
            },
        },
    )

    assert decision is not None
    assert decision.action is ReviewAction.REPLAN
    assert decision.revision is not None
    assert decision.revision.keep_task_ids == ("draft:run-1",)
    assert decision.revision.new_tasks[0]["depends_on"] == ["draft:run-1"]
