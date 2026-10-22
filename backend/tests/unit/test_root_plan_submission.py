from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.orchestration.root_workflow import (
    RootWorkflow,
    _deferred_version_review_decision,
)
from domain.orchestration import OrchestrationCommandType, ReviewAction, ReviewContext


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


class ShellToolService(ToolService):
    async def list_tools(self):
        return [
            *await super().list_tools(),
            SimpleNamespace(
                name="exec_shell",
                description="Run a command in the sandbox",
                parameters={"type": "object"},
                risk_level="high",
                execution_mode="native",
                enabled=True,
                require_approval=True,
            ),
        ]


class CommandHandler:
    def __init__(self):
        self.submissions = []

    async def submit_plan(self, command, plan, tasks, *, legal_tools=None):
        self.submissions.append((command, plan, tasks, legal_tools))


@pytest.mark.asyncio
async def test_root_submits_initial_plan_through_atomic_command_handler():
    handler = CommandHandler()
    workflow = RootWorkflow(
        SimpleNamespace(
            tool_service=ToolService(),
            orchestration_command_handler=handler,
            event_publisher=None,
            plan_builder=None,
        )
    )

    result = await workflow.materialize_execution_plan(
        {
            "request": {
                "run_id": "run-42",
                "session_id": "session-9",
                "user_message": "Prepare a report, but delay Version 2 until after Root Review.",
            },
            "understanding": {
                "task_spec": {
                    "goal": "Prepare a report, but delay Version 2 until after Root Review."
                }
            },
            "plan": {
                "plan_id": "report-plan",
                "goal": "Prepare a report",
                "max_parallelism": 2,
                "edges": [["gather", "summarize"]],
                "tasks": [
                    {
                        "task_id": "gather",
                        "title": "Gather data",
                        "objective": "Collect local source data",
                        "allowed_tools": ["read_local_file", "disabled_tool"],
                        "timeout_seconds": 42,
                    },
                    {
                        "task_id": "summarize",
                        "title": "Summarize data",
                        "objective": "Summarize the collected source data",
                        "allowed_tools": [],
                    },
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
    assert plan.goal == "Prepare a report, but delay Version 2 until after Root Review."
    assert tasks[0].task_id == "gather:run-42"
    assert tasks[0].allowed_tools == ("read_local_file",)
    assert tasks[0].timeout_seconds == 42
    assert [(edge.from_task_id, edge.to_task_id) for edge in plan.edges] == [
        ("gather:run-42", "summarize:run-42")
    ]
    assert legal_tools == {"read_local_file"}
    assert result["plan"]["plan_id"] == plan.plan_id


@pytest.mark.asyncio
async def test_explicit_user_tool_allowlist_binds_to_matching_task_input():
    handler = CommandHandler()
    workflow = RootWorkflow(
        SimpleNamespace(
            tool_service=ShellToolService(),
            orchestration_command_handler=handler,
            event_publisher=None,
            plan_builder=None,
        )
    )

    await workflow.materialize_execution_plan(
        {
            "request": {"run_id": "run-shell", "session_id": "session-1"},
            "understanding": {
                "task_spec": {"goal": "run one command", "allowed_tools": ["exec_shell"]}
            },
            "plan": {
                "plan_id": "shell-plan",
                "goal": "run one command",
                "tasks": [
                    {
                        "task_id": "version",
                        "title": "Python version",
                        "objective": "Call exec_shell once and return stdout",
                        "input": {
                            "exec_shell": {
                                "command": "python -c 'import sys; print(sys.version.split()[0])'"
                            }
                        },
                        "allowed_tools": [],
                    }
                ],
            },
        }
    )

    _, _plan, tasks, legal_tools = handler.submissions[0]
    assert tasks[0].allowed_tools == ("exec_shell",)
    assert "exec_shell" in legal_tools


def test_completed_version_review_automatically_submits_deferred_version():
    context = ReviewContext(
        original_goal="Version 1 drafts; Version 2 reviews after Root Review",
        plan_id="plan:run-1",
        plan_version=1,
        round_id="round-1",
        full_dag_summary={},
        round_results=({"task_id": "draft:run-1", "status": "completed"},),
    )
    decision = _deferred_version_review_decision(
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


def test_later_version_review_keeps_all_tasks_from_current_dag():
    context = ReviewContext(
        original_goal="Version 3 produces final output",
        plan_id="plan:run-1",
        plan_version=2,
        round_id="round-2",
        full_dag_summary={
            "nodes": [
                {"task_id": "draft:run-1"},
                {"task_id": "review:run-1"},
            ]
        },
        previous_results_summary=(
            {"task_id": "draft:run-1", "status": "completed"},
        ),
        round_results=({"task_id": "review:run-1", "status": "completed"},),
    )
    decision = _deferred_version_review_decision(
        context,
        {
            "request": {"run_id": "run-1"},
            "plan": {"task_ids": ["draft:run-1"]},
            "deferred_plan_versions": {
                "3": [
                    {
                        "task_id": "finalize",
                        "title": "Finalize",
                        "objective": "Produce final output",
                        "depends_on": ["review"],
                    }
                ]
            },
        },
    )

    assert decision is not None and decision.revision is not None
    assert decision.revision.keep_task_ids == ("draft:run-1", "review:run-1")
    assert decision.revision.new_tasks[0]["depends_on"] == ["review:run-1"]


def test_deferred_tasks_can_depend_on_another_task_in_the_same_version():
    context = ReviewContext(
        original_goal="Version 2 contains a review and a summary",
        plan_id="plan:run-1",
        plan_version=1,
        round_id="round-1",
        full_dag_summary={},
        round_results=({"task_id": "draft:run-1", "status": "completed"},),
    )

    decision = _deferred_version_review_decision(
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
                    },
                    {
                        "task_id": "summary",
                        "title": "Summary",
                        "objective": "Summarize the review",
                        "depends_on": ["review"],
                    },
                ]
            },
        },
    )

    assert decision is not None and decision.revision is not None
    assert decision.revision.new_tasks[1]["depends_on"] == ["review:run-1"]


def test_deferred_version_with_unknown_dependency_is_not_submitted():
    context = ReviewContext(
        original_goal="Version 2 reviews",
        plan_id="plan:run-1",
        plan_version=1,
        round_id="round-1",
        full_dag_summary={},
        round_results=({"task_id": "draft:run-1", "status": "completed"},),
    )

    decision = _deferred_version_review_decision(
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
                        "depends_on": ["missing"],
                    }
                ]
            },
        },
    )

    assert decision is None


@pytest.mark.asyncio
async def test_root_review_overrides_continue_when_deferred_version_is_due():
    context = ReviewContext(
        original_goal="Version 1 drafts; Version 2 reviews after Root Review",
        plan_id="plan:run-1",
        plan_version=1,
        round_id="round-1",
        full_dag_summary={},
        round_results=({"task_id": "draft:run-1", "status": "completed"},),
    )

    class ReviewContexts:
        async def load_review_context(self, _round_id):
            return context

    class Agent:
        async def review(self, *_args, **_kwargs):
            raise AssertionError("deferred version review must be deterministic")

    class Commands:
        def __init__(self):
            self.decision = None

        async def load_review_decision(self, **_kwargs):
            return None

        async def apply_review_decision(self, _command, decision):
            self.decision = decision
            return SimpleNamespace(status="accepted")

    commands = Commands()
    workflow = RootWorkflow(
        SimpleNamespace(
            agent=Agent(),
            review_context_repository=ReviewContexts(),
            orchestration_command_handler=commands,
        )
    )
    identity = {
        "run_id": "run-1",
        "plan_id": "plan:run-1",
        "round_id": "round-1",
        "plan_version": 1,
    }

    result = await workflow.review_completed_round(
        {
            "request": {"run_id": "run-1"},
            "root_wait": identity,
            "resume_value": {"type": "round_completed", **identity},
            "plan": {"task_ids": ["draft:run-1"]},
            "deferred_plan_versions": {
                "2": [
                    {
                        "task_id": "review",
                        "title": "Review",
                        "objective": "Review the draft",
                        "depends_on": ["draft"],
                    }
                ]
            },
        }
    )

    assert result["review_action"] == "REPLAN"
    assert commands.decision.action is ReviewAction.REPLAN
